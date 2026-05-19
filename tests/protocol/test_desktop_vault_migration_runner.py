"""T9.3 + T9.4 — Vault migration runner end-to-end."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
)
from src.vault.migration.state import load_state, save_state, MigrationRecord  # noqa: E402
from src.vault.migration.runner import (  # noqa: E402
    MigrationVerifyOutcome,
    rollback_verified_migration,
    run_migration,
)
from src.vault.upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import (  # noqa: E402
    FakeUploadRelay,
    seed_sharded_state,
)


VAULT_ACCESS_SECRET = "vault-secret"
SOURCE_URL = "https://source.example.com/SERVICES/dc"
TARGET_URL = "https://target.example.com/SERVICES/dc"


# ---------------------------------------------------------------------------
# A relay fake that supports both upload + migration semantics. We layer the
# migration methods on top of the existing FakeUploadRelay so chunk + sharded
# manifest logic stays identical between upload and migration tests.
# ---------------------------------------------------------------------------


class FakeMigrationRelay(FakeUploadRelay):
    def __init__(self, *, vault_id: str = VAULT_ID) -> None:
        super().__init__()
        self.vault_id = vault_id
        self.vault_created = False
        self.vault_access_token_hash: bytes = b""
        self.encrypted_header: bytes = b""
        self.header_hash: str = ""
        self.header_revision: int = 1
        self.migrated_to: str | None = None
        self.migration_intents: dict[str, dict] = {}

    # --- target-side: create_vault, get_header --------------------------

    def create_vault(
        self,
        vault_id: str,
        vault_access_token_hash: bytes,
        encrypted_header: bytes,
        header_hash: str,
        initial_root_ciphertext: bytes,
        initial_root_hash: str,
        *,
        initial_root_revision: int | None = None,
        initial_header_revision: int | None = None,
    ) -> dict:
        if self.vault_created:
            # Idempotent re-entry on retry — runner expects RuntimeError
            # with "vault_already_exists" in the message.
            raise RuntimeError("vault_already_exists: target vault already present")
        self.vault_created = True
        self.vault_id = vault_id
        self.vault_access_token_hash = bytes(vault_access_token_hash)
        self.encrypted_header = bytes(encrypted_header)
        self.header_hash = header_hash
        self.header_revision = int(initial_header_revision or 1)
        # Mirror the inherited sharded-surface slots so subsequent
        # ``get_root`` / ``put_shard`` calls see the seeded state. The
        # legacy ``current_envelope`` / ``current_revision`` slots are
        # intentionally left untouched — the sharded runner never
        # reads them.
        self.root_envelope = bytes(initial_root_ciphertext)
        self.root_hash = initial_root_hash
        self.root_revision = int(initial_root_revision or 1)
        return {
            "vault_id": vault_id,
            "header_revision": self.header_revision,
            "root_revision": self.root_revision,
        }

    def get_header(self, vault_id, vault_access_secret) -> dict:
        return {
            "vault_id": vault_id,
            "encrypted_header": self.encrypted_header,
            "header_hash": self.header_hash,
            "header_revision": self.header_revision,
            "quota_ciphertext_bytes": 1073741824,
            "used_ciphertext_bytes": sum(len(v) for v in self.chunks.values()),
            "migrated_to": self.migrated_to,
        }

    # --- source-side: migration_start / verify_source / commit --------------

    def migration_start(self, vault_id, vault_access_secret, *, target_relay_url) -> dict:
        existing = self.migration_intents.get(vault_id)
        if existing is not None:
            if existing["target_relay_url"] != target_relay_url:
                raise RuntimeError("vault_migration_in_progress: different target")
            return {
                "vault_id": vault_id,
                "target_relay_url": existing["target_relay_url"],
                "started_at": existing["started_at"],
                "token": None,
                "token_returned": False,
            }
        token = "mig_v1_" + ("x" * 30)
        self.migration_intents[vault_id] = {
            "target_relay_url": target_relay_url,
            "started_at": "2026-05-04T10:00:00Z",
            "token": token,
        }
        return {
            "vault_id": vault_id,
            "target_relay_url": target_relay_url,
            "started_at": "2026-05-04T10:00:00Z",
            "token": token,
            "token_returned": True,
        }

    def migration_verify_source(self, vault_id, vault_access_secret) -> dict:
        intent = self.migration_intents.get(vault_id)
        if intent is None:
            raise RuntimeError("vault_invalid_request: no intent")
        # Mirror the server's sharded verify-source payload: root_hash
        # + per-folder shard_hashes map. The chunk_count is the
        # union-of-shards distinct count.
        shard_hashes = {
            rf_id: str(state.get("hash") or "")
            for rf_id, state in self.shards.items()
        }
        return {
            "vault_id": vault_id,
            "root_revision": int(self.root_revision),
            "root_hash": self.root_hash,
            "shard_hashes": shard_hashes,
            "chunk_count": len(self.chunks),
            "used_ciphertext_bytes": sum(len(v) for v in self.chunks.values()),
            "target_relay_url": intent["target_relay_url"],
            "started_at": intent["started_at"],
        }

    def migration_commit(self, vault_id, vault_access_secret, *, target_relay_url) -> dict:
        intent = self.migration_intents.get(vault_id)
        if intent is None:
            raise RuntimeError("vault_invalid_request: no intent to commit")
        if intent["target_relay_url"] != target_relay_url:
            raise RuntimeError("vault_migration_in_progress: target mismatch")
        self.migrated_to = target_relay_url
        return {
            "vault_id": vault_id,
            "target_relay_url": target_relay_url,
            "committed_at": "2026-05-04T10:05:00Z",
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class VaultMigrationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_migration_runner_"))
        self.config_dir = self.tmpdir / "config"
        self.config_dir.mkdir(parents=True)
        # Redirect resume cache (upload sessions stamp under
        # XDG_CACHE_HOME) so the migration test doesn't pollute the real
        # ~/.cache directory while seeding the source vault via upload_file.
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_migration_copies_chunks_and_publishes_state(self) -> None:
        """T9.3 + T9.4: source vault with two files migrates verbatim."""
        source_relay, source_root_at_t0 = self._populated_source(files={
            "alpha.txt": b"alpha content for migration",
            "beta.bin": b"beta binary blob, distinct content",
        })
        target_relay = FakeMigrationRelay()

        vault = _vault()
        try:
            result = run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
            )
        finally:
            vault.close()

        self.assertTrue(result.verify.matches, result.verify.mismatches)
        self.assertGreater(result.chunks_copied, 0)
        # All source chunks now on target.
        self.assertEqual(set(target_relay.chunks), set(source_relay.chunks))
        # Root envelope verbatim.
        self.assertEqual(target_relay.root_envelope, source_relay.root_envelope)
        self.assertEqual(target_relay.root_revision, source_relay.root_revision)
        self.assertEqual(target_relay.root_hash, source_relay.root_hash)
        # Each per-folder shard envelope copied verbatim too.
        self.assertEqual(set(target_relay.shards), set(source_relay.shards))
        for rf_id, src_state in source_relay.shards.items():
            tgt_state = target_relay.shards[rf_id]
            self.assertEqual(tgt_state["envelope"], src_state["envelope"])
            self.assertEqual(tgt_state["hash"], src_state["hash"])
        # Source has been committed into read-only / migrated state.
        self.assertEqual(source_relay.migrated_to, TARGET_URL)
        # State file gone (post-commit cleanup).
        self.assertIsNone(load_state(self.config_dir))
        # Verification did sample some chunks and they all decrypted.
        self.assertGreater(result.verify.sample_size, 0)
        self.assertEqual(result.verify.sample_size, result.verify.sample_passed)

    def test_resumable_after_partial_copy(self) -> None:
        """T9.3 acceptance: a crash mid-copy resumes without re-uploading
        chunks the target already has."""
        source_relay, _ = self._populated_source(files={
            "x.txt": b"first chunk content",
            "y.txt": b"second chunk content distinct",
        })
        target_relay = FakeMigrationRelay()

        # Pre-seed target with an in-progress state file + half the chunks.
        # A real client would land here when the process died after some
        # but not all PUTs succeeded.
        vault = _vault()
        try:
            # Save a "copying" state file so the runner doesn't restart
            # from idle (which would replay /migration/start and could
            # re-issue a token).
            seed_record = MigrationRecord(
                vault_id=VAULT_ID, state="copying",
                source_relay_url=SOURCE_URL, target_relay_url=TARGET_URL,
                started_at="2026-05-04T10:00:00.000Z",
                migration_token="mig_v1_" + "x" * 30,
            )
            save_state(seed_record, self.config_dir)
            # Real source relay would already have the intent row from
            # the prior interrupted /start call; replay that here.
            source_relay.migration_intents[VAULT_ID] = {
                "target_relay_url": TARGET_URL,
                "started_at": "2026-05-04T10:00:00Z",
                "token": "mig_v1_" + "x" * 30,
            }
            # Bootstrap target as the runner would have done before crashing.
            token_hash = hashlib.sha256(VAULT_ACCESS_SECRET.encode("ascii")).digest()
            target_relay.create_vault(
                VAULT_ID,
                token_hash,
                b"hdr",
                "h" * 64,
                source_relay.root_envelope,
                source_relay.root_hash,
                initial_root_revision=source_relay.root_revision,
                initial_header_revision=1,
            )
            # Pre-place one chunk.
            first_cid = next(iter(source_relay.chunks))
            target_relay.chunks[first_cid] = source_relay.chunks[first_cid]

            result = run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
            )
        finally:
            vault.close()

        self.assertTrue(result.verify.matches, result.verify.mismatches)
        self.assertGreater(result.chunks_skipped, 0)  # at least the pre-placed one
        self.assertEqual(set(target_relay.chunks), set(source_relay.chunks))
        self.assertEqual(source_relay.migrated_to, TARGET_URL)

    def test_on_committed_callback_runs_before_clear_state(self) -> None:
        """F-C15: ``on_committed`` lets the caller persist
        ``previous_relay_url`` BEFORE the runner deletes the state
        file. The callback receives the committed-state record so it
        can read ``record.previous_relay_url`` and write the matching
        config field atomically.
        """
        from src.vault.migration.state import load_state

        source_relay, _ = self._populated_source(
            files={"k.txt": b"committed callback test"},
        )
        target_relay = FakeMigrationRelay()

        captured_records: list = []
        # When the callback runs, the state file MUST still exist —
        # this is the contract: callback first, clear_state second.
        state_present_during_callback = {"flag": None}

        def callback(record):
            captured_records.append(record)
            state_present_during_callback["flag"] = (
                load_state(self.config_dir) is not None
            )

        vault = _vault()
        try:
            run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
                on_committed=callback,
            )
        finally:
            vault.close()

        self.assertEqual(len(captured_records), 1)
        self.assertEqual(captured_records[0].state, "committed")
        self.assertEqual(
            captured_records[0].previous_relay_url, SOURCE_URL,
        )
        self.assertTrue(
            state_present_during_callback["flag"],
            "state file must still exist when on_committed runs",
        )
        # After return, the state file is cleared.
        self.assertIsNone(load_state(self.config_dir))

    def test_on_committed_callback_failure_keeps_state_for_retry(self) -> None:
        """F-C15: if the callback raises, the state file stays at
        ``committed`` so a later ``run_migration`` retries the
        callback. Without this gate, a config-write crash would
        silently lose the rollback URL forever.
        """
        from src.vault.migration.state import load_state

        source_relay, _ = self._populated_source(
            files={"k.txt": b"flaky callback test"},
        )
        target_relay = FakeMigrationRelay()

        attempts = {"n": 0}

        def flaky_callback(record):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("config write fell over")
            # Second call succeeds.

        vault = _vault()
        try:
            with self.assertLogs(
                "src.vault.migration.runner", level="WARNING",
            ) as captured:
                run_migration(
                    vault=vault,
                    source_relay=source_relay,
                    target_relay=target_relay,
                    source_relay_url=SOURCE_URL,
                    target_relay_url=TARGET_URL,
                    config_dir=self.config_dir,
                    on_committed=flaky_callback,
                )
        finally:
            vault.close()

        # First run: state survived because callback raised.
        record_after_first = load_state(self.config_dir)
        self.assertIsNotNone(record_after_first)
        self.assertEqual(record_after_first.state, "committed")
        self.assertTrue(
            any(
                "committed_callback_failed" in line
                for line in captured.output
            ),
            f"missing callback_failed warning: {captured.output!r}",
        )

        # Re-run picks up at ``committed`` and the second-attempt
        # callback succeeds, so state clears.
        vault = _vault()
        try:
            run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
                on_committed=flaky_callback,
            )
        finally:
            vault.close()
        self.assertEqual(attempts["n"], 2)
        self.assertIsNone(load_state(self.config_dir))

    def test_committed_to_idle_warns_on_source_drift(self) -> None:
        """F-C09: between two ``run_migration`` calls an operator can
        rollback the migration on the source side. The desktop's
        local state still says ``committed``; the next run drives
        ``committed → idle`` and clears the state file. Without a
        forensic check the drift is silent. The audit helper logs
        ``vault.migration.committed_source_drift`` (warning) when the
        source's ``migration_verify_source`` reports a different
        target than the one we committed to.
        """
        from src.vault.migration.state import MigrationRecord, save_state

        source_relay, _ = self._populated_source(
            files={"k.txt": b"committed content"},
        )
        target_relay = FakeMigrationRelay()

        # Drive the first run all the way through commit.
        vault = _vault()
        try:
            run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
            )
        finally:
            vault.close()

        # Force the local state back to ``committed`` (the post-commit
        # cleanup may have already advanced it to idle); also point
        # the source's intent at a *different* URL to simulate an
        # operator-driven rollback that re-pointed the migration at a
        # different target between runs.
        record = MigrationRecord(
            vault_id=VAULT_ID, state="committed",
            source_relay_url=SOURCE_URL, target_relay_url=TARGET_URL,
            started_at="2026-05-04T10:00:00.000Z",
            verified_at="2026-05-04T10:01:00.000Z",
            committed_at="2026-05-04T10:02:00.000Z",
        )
        save_state(record, self.config_dir)
        source_relay.migration_intents[VAULT_ID] = {
            "target_relay_url": "https://different-target.example",
            "started_at": "2026-05-04T11:00:00Z",
            "token": "mig_v1_" + "y" * 30,
        }

        vault = _vault()
        try:
            with self.assertLogs(
                "src.vault.migration.runner", level="WARNING",
            ) as captured:
                run_migration(
                    vault=vault,
                    source_relay=source_relay,
                    target_relay=target_relay,
                    source_relay_url=SOURCE_URL,
                    target_relay_url=TARGET_URL,
                    config_dir=self.config_dir,
                )
        finally:
            vault.close()

        self.assertTrue(
            any(
                "committed_source_drift" in line
                and "different-target.example" in line
                for line in captured.output
            ),
            f"missing committed_source_drift warning in: {captured.output!r}",
        )
        # The state still cleared (drift is observability, not blocking).
        from src.vault.migration.state import load_state
        self.assertIsNone(load_state(self.config_dir))

    def test_committed_to_idle_silent_when_source_aligns(self) -> None:
        """F-C09 negative case: when the source still reports the
        same target we committed to, no drift warning fires.
        """
        source_relay, _ = self._populated_source(
            files={"k.txt": b"aligned content"},
        )
        target_relay = FakeMigrationRelay()
        vault = _vault()
        try:
            with self.assertLogs(
                "src.vault.migration.runner", level="DEBUG",
            ) as captured:
                run_migration(
                    vault=vault,
                    source_relay=source_relay,
                    target_relay=target_relay,
                    source_relay_url=SOURCE_URL,
                    target_relay_url=TARGET_URL,
                    config_dir=self.config_dir,
                )
        finally:
            vault.close()
        # No warning-level drift line.
        self.assertFalse(
            any("committed_source_drift" in line for line in captured.output),
        )

    def test_verify_detects_chunk_count_drift_directly(self) -> None:
        """F-C06: a partial copy where the target's chunk count is
        less than the source's must trip ``chunk_count`` in
        ``mismatches`` even when the random AEAD sample passes.

        Pre-fix the proxy ``if src_chunks and sample_size_actual == 0``
        only fired when the *target manifest* had zero chunks, so a
        target with N-1 chunks would pass quietly when sample_size <= N-1.
        We exercise that by running the orchestrator and then deleting
        one chunk from the target's relay state before the verify
        pass.
        """
        from src.vault.migration.state import (
            MigrationRecord, save_state,
        )

        source_relay, _ = self._populated_source(files={
            "a.txt": b"alpha bytes",
            "b.txt": b"beta bytes",
            "c.txt": b"gamma bytes",
        })
        target_relay = FakeMigrationRelay()

        vault = _vault()
        try:
            result1 = run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
            )
            self.assertTrue(result1.verify.matches)  # baseline run completes
        finally:
            vault.close()

        # Drop one chunk from the target relay AND from the target's
        # shard plaintext (re-publish the matching folder shard with
        # the dropped chunk excised). The runner's
        # ``_count_unique_chunks_in_shards`` walks the target's shards,
        # so we need a fresh shard envelope to reflect the deletion.
        dropped_cid = next(iter(target_relay.chunks))
        del target_relay.chunks[dropped_cid]

        # Re-publish each folder shard with the dropped chunk removed.
        vault = _vault()
        try:
            target_root = vault.fetch_root_manifest(target_relay)
            for pointer in target_root.get("remote_folders", []) or []:
                rf_id = str(pointer.get("remote_folder_id") or "")
                if not rf_id:
                    continue
                if int(pointer.get("shard_revision") or 0) <= 0:
                    continue
                shard = vault.fetch_folder_shard(target_relay, rf_id)
                changed = False
                for entry in shard.get("entries", []) or []:
                    for version in entry.get("versions", []) or []:
                        original = version.get("chunks", []) or []
                        filtered = [
                            c for c in original
                            if str(c.get("chunk_id")) != dropped_cid
                        ]
                        if len(filtered) != len(original):
                            version["chunks"] = filtered
                            changed = True
                if not changed:
                    continue
                # Bump the shard chain + root pointer atomically.
                shard_rev = int(shard["shard_revision"])
                shard["shard_revision"] = shard_rev + 1
                shard["parent_shard_revision"] = shard_rev
                shard["created_at"] = "2026-05-04T10:30:00.000Z"
                shard["author_device_id"] = AUTHOR
                root_rev = int(target_root["root_revision"])
                next_root = dict(target_root)
                next_root["root_revision"] = root_rev + 1
                next_root["parent_root_revision"] = root_rev
                next_root["created_at"] = "2026-05-04T10:30:00.000Z"
                next_root["author_device_id"] = AUTHOR
                vault.publish_shard_with_root(target_relay, rf_id, shard, next_root)
                target_root = vault.fetch_root_manifest(target_relay)
        finally:
            vault.close()

        # Re-run from "verified" so verify is the only step that runs.
        record = MigrationRecord(
            vault_id=VAULT_ID, state="verified",
            source_relay_url=SOURCE_URL, target_relay_url=TARGET_URL,
            started_at="2026-05-04T10:00:00.000Z",
            verified_at="2026-05-04T10:01:00.000Z",
        )
        save_state(record, self.config_dir)
        source_relay.migrated_to = None
        source_relay.migration_intents.setdefault(VAULT_ID, {
            "target_relay_url": TARGET_URL,
            "started_at": "2026-05-04T10:00:00Z",
            "token": "mig_v1_" + "x" * 30,
        })
        vault = _vault()
        try:
            result2 = run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
            )
        finally:
            vault.close()

        self.assertFalse(result2.verify.matches)
        self.assertIn("chunk_count", result2.verify.mismatches)
        self.assertNotEqual(source_relay.migrated_to, TARGET_URL)

    def test_verify_failure_short_circuits_before_commit(self) -> None:
        """T9.4: tampered chunks on the target produce mismatches and
        prevent the commit transition."""
        source_relay, _ = self._populated_source(files={"k.txt": b"verify content"})
        target_relay = FakeMigrationRelay()

        vault = _vault()
        try:
            # Run far enough to set up target, but tamper one chunk before
            # verify to force the sample-decrypt to fail.
            from src.vault.migration.state import save_state, MigrationRecord

            record = MigrationRecord(
                vault_id=VAULT_ID, state="started",
                source_relay_url=SOURCE_URL, target_relay_url=TARGET_URL,
                started_at="2026-05-04T10:00:00.000Z",
            )
            save_state(record, self.config_dir)
            # Run the actual orchestrator; we tamper *after* it copies
            # but *before* the source-internal verify resolves… in
            # practice we have to inject the tamper post-copy and
            # re-run verify. Easiest: copy first, tamper, then call
            # run_migration which picks up at "verified".
            result1 = run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
            )
            self.assertTrue(result1.verify.matches)  # baseline run completes

        finally:
            vault.close()

        # Now tamper a chunk on the target and re-run; it should detect
        # the divergence on the next verify pass. We simulate this by
        # writing a fresh "verified" state and corrupting one chunk.
        first_cid = next(iter(target_relay.chunks))
        target_relay.chunks[first_cid] = b"\x00" * 200
        record = MigrationRecord(
            vault_id=VAULT_ID, state="verified",
            source_relay_url=SOURCE_URL, target_relay_url=TARGET_URL,
            started_at="2026-05-04T10:00:00.000Z",
            verified_at="2026-05-04T10:01:00.000Z",
        )
        save_state(record, self.config_dir)
        # Restore a fresh source intent so commit would be allowed if
        # verify succeeded — but it won't.
        source_relay.migrated_to = None
        source_relay.migration_intents.setdefault(VAULT_ID, {
            "target_relay_url": TARGET_URL,
            "started_at": "2026-05-04T10:00:00Z",
            "token": "mig_v1_" + "x" * 30,
        })
        vault = _vault()
        try:
            result2 = run_migration(
                vault=vault,
                source_relay=source_relay,
                target_relay=target_relay,
                source_relay_url=SOURCE_URL,
                target_relay_url=TARGET_URL,
                config_dir=self.config_dir,
            )
        finally:
            vault.close()

        self.assertFalse(result2.verify.matches)
        self.assertIn("chunk_sample", result2.verify.mismatches)
        # Source should NOT have flipped to committed.
        self.assertNotEqual(source_relay.migrated_to, TARGET_URL)

    # ------------------------------------------------------------------

    def _populated_source(self, *, files: dict[str, bytes]) -> tuple[FakeMigrationRelay, bytes]:
        """Build a source relay seeded with ``files`` already uploaded.

        Sharded shape: the source relay's ``root_envelope`` and
        per-folder ``shards`` are populated via the same Phase-H
        helpers the live sync engine uses. The legacy unified manifest
        slots are intentionally left untouched — the migration runner
        only reads the sharded surface.
        """
        manifest = _empty_manifest()
        relay = FakeMigrationRelay()
        # Mark the vault as created on the source side; the migration
        # runner never calls create_vault on the source, but the
        # fake's get_header reads the header_revision slot.
        relay.vault_created = True
        relay.header_revision = 1
        relay.encrypted_header = b"src-header-bytes"
        relay.header_hash = "h" * 64
        vault = _vault()
        try:
            # Seed the sharded root + shard for the empty manifest's
            # ``DOCS_ID`` folder so ``upload_file`` has a base shard
            # revision to publish on top of.
            seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
            current_manifest = manifest
            for path, content in files.items():
                local = self.tmpdir / path.replace("/", "_")
                local.write_bytes(content)
                res = upload_file(
                    vault=vault, relay=relay,
                    manifest=current_manifest,
                    local_path=local, remote_folder_id=DOCS_ID,
                    remote_path=path, author_device_id=AUTHOR,
                )
                current_manifest = assemble_unified_manifest(res.root, {res.remote_folder_id: res.shard})
        finally:
            vault.close()
        return relay, relay.root_envelope


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID, master_key=MASTER_KEY,
        recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1, manifest_revision=1,
        manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
    )


def _empty_manifest() -> dict:
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=1, parent_root_revision=0,
        created_at="2026-05-01T10:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-01T10:00:00.000Z",
                created_by_device_id=AUTHOR,
            )
        ],
    )
    shard = make_folder_shard(
        vault_id=VAULT_ID,
        remote_folder_id=DOCS_ID,
        shard_revision=1, parent_shard_revision=0,
        created_at="2026-05-01T10:00:00.000Z",
        author_device_id=AUTHOR,
        entries=[],
    )
    return assemble_unified_manifest(root, {DOCS_ID: shard})


class RollbackVerifiedMigrationTests(unittest.TestCase):
    """F-C21 polish — explicit ``verified → idle`` rollback helper.

    ``run_migration`` is idempotent: re-invoking on a record at
    ``verified`` re-runs the verify step and lands the same mismatch
    every time. There's no built-in path from ``verified`` back to
    ``idle`` short of editing the JSON state file. This helper plugs
    that gap so the user can abort cleanly when verify is permanently
    failing.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_migration_rollback_"))
        self.config_dir = self.tmpdir / "config"
        self.config_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_None_when_no_state_file(self) -> None:
        self.assertIsNone(rollback_verified_migration(self.config_dir))

    def test_clears_state_when_at_verified(self) -> None:
        record = MigrationRecord(
            vault_id=VAULT_ID,
            state="verified",
            source_relay_url=SOURCE_URL,
            target_relay_url=TARGET_URL,
            started_at="2026-05-04T10:00:00.000Z",
            verified_at="2026-05-04T10:01:00.000Z",
        )
        save_state(record, self.config_dir)

        rolled = rollback_verified_migration(self.config_dir)

        self.assertIsNotNone(rolled)
        self.assertEqual(rolled.state, "idle")
        # State file gone — a relaunch sees no in-flight migration.
        self.assertIsNone(load_state(self.config_dir))

    def test_rejects_non_verified_state(self) -> None:
        record = MigrationRecord(
            vault_id=VAULT_ID,
            state="copying",
            source_relay_url=SOURCE_URL,
            target_relay_url=TARGET_URL,
            started_at="2026-05-04T10:00:00.000Z",
            migration_token="mig_v1_" + "x" * 30,
        )
        save_state(record, self.config_dir)
        with self.assertRaises(ValueError):
            rollback_verified_migration(self.config_dir)
        # State file untouched on refusal.
        persisted = load_state(self.config_dir)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.state, "copying")


if __name__ == "__main__":
    unittest.main()
