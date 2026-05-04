"""T11.2 — Restore remote folder one-shot."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_manifest import (  # noqa: E402
    make_manifest,
    make_remote_folder,
    tombstone_file_entry,
)
from src.vault_restore import (  # noqa: E402
    RestoreResult,
    restore_remote_folder,
    restore_remote_folder_at_date,
)
from src.vault_upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import FakeUploadRelay  # noqa: E402


VAULT_ACCESS_SECRET = "vault-secret"
DEVICE_NAME = "Workstation 7"
WHEN = datetime(2026, 5, 4, 17, 30, tzinfo=timezone.utc)


class RestoreRemoteFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_restore_test_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.dest = self.tmpdir / "restore_target"
        self.dest.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_remote(self, files: dict[str, bytes], *, with_tombstone: str | None = None) -> tuple[FakeUploadRelay, dict]:
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-04T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[],
                ),
            ],
        )
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            current = manifest
            for path, content in files.items():
                local = self.tmpdir / "src_" / path.replace("/", "_")
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(content)
                res = upload_file(
                    vault=vault, relay=relay, manifest=current,
                    local_path=local, remote_folder_id=DOCS_ID,
                    remote_path=path, author_device_id=AUTHOR,
                )
                current = res.manifest
            if with_tombstone is not None:
                current = tombstone_file_entry(
                    current,
                    remote_folder_id=DOCS_ID,
                    path=with_tombstone,
                    deleted_at="2026-05-04T13:00:00.000Z",
                    author_device_id=AUTHOR,
                )
                current["revision"] = int(current["revision"]) + 1
                current["parent_revision"] = current["revision"] - 1
                vault.publish_manifest(relay, current)
        finally:
            vault.close()
        from src.vault_browser_model import decrypt_manifest as _decrypt
        observer = _vault()
        try:
            published = _decrypt(observer, relay.current_envelope)
        finally:
            observer.close()
        return relay, published

    # ------------------------------------------------------------------
    # Acceptance: empty destination materializes cleanly
    # ------------------------------------------------------------------

    def test_restore_into_empty_path_materializes_every_file(self) -> None:
        relay, manifest = self._seed_remote({
            "alpha.txt": b"alpha",
            "nested/beta.bin": b"\x01\x02\x03",
        })
        vault = _vault()
        try:
            result = restore_remote_folder(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN,
            )
        finally:
            vault.close()

        self.assertEqual(set(result.written), {"alpha.txt", "nested/beta.bin"})
        self.assertEqual(result.skipped_identical, [])
        self.assertEqual(result.conflict_copies, [])
        self.assertEqual((self.dest / "alpha.txt").read_bytes(), b"alpha")
        self.assertEqual(
            (self.dest / "nested" / "beta.bin").read_bytes(), b"\x01\x02\x03",
        )

    # ------------------------------------------------------------------
    # Acceptance: restore into populated path with collision uses A20
    # ------------------------------------------------------------------

    def test_restore_into_populated_path_uses_a20_naming_for_collisions(self) -> None:
        relay, manifest = self._seed_remote({
            "report.docx": b"remote bytes",
        })
        # Pre-place a *different* file at the same path locally.
        (self.dest / "report.docx").write_bytes(b"local bytes (do not lose)")

        vault = _vault()
        try:
            result = restore_remote_folder(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN,
            )
        finally:
            vault.close()

        # Local file preserved verbatim.
        self.assertEqual(
            (self.dest / "report.docx").read_bytes(),
            b"local bytes (do not lose)",
        )
        # Restored bytes landed at an §A20 conflict path.
        self.assertEqual(result.written, [])
        self.assertEqual(len(result.conflict_copies), 1)
        original, conflict = result.conflict_copies[0]
        self.assertEqual(original, "report.docx")
        self.assertEqual(
            conflict,
            "report (conflict restored Workstation 7 2026-05-04 17-30).docx",
        )
        self.assertTrue((self.dest / conflict).exists())
        self.assertEqual(
            (self.dest / conflict).read_bytes(), b"remote bytes",
        )

    # ------------------------------------------------------------------
    # Acceptance: identical content is skipped
    # ------------------------------------------------------------------

    def test_identical_local_content_is_skipped(self) -> None:
        payload = b"exact same bytes locally and remotely"
        relay, manifest = self._seed_remote({"twin.txt": payload})
        (self.dest / "twin.txt").write_bytes(payload)

        vault = _vault()
        try:
            result = restore_remote_folder(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN,
            )
        finally:
            vault.close()

        self.assertEqual(result.written, [])
        self.assertEqual(result.skipped_identical, ["twin.txt"])
        self.assertEqual(result.conflict_copies, [])
        # No conflict copy alongside.
        siblings = sorted(p.name for p in self.dest.iterdir())
        self.assertEqual(siblings, ["twin.txt"])

    # ------------------------------------------------------------------
    # Tombstones never materialize
    # ------------------------------------------------------------------

    def test_tombstones_are_not_materialized(self) -> None:
        relay, manifest = self._seed_remote(
            {"keep.txt": b"keep me", "ghost.txt": b"will be tombstoned"},
            with_tombstone="ghost.txt",
        )
        vault = _vault()
        try:
            result = restore_remote_folder(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN,
            )
        finally:
            vault.close()

        self.assertEqual(result.written, ["keep.txt"])
        self.assertFalse((self.dest / "ghost.txt").exists())

    # ------------------------------------------------------------------
    # Disk preflight raises when not enough room
    # ------------------------------------------------------------------

    def test_preflight_raises_when_disk_full(self) -> None:
        relay, manifest = self._seed_remote({"a.bin": b"x" * 1024})
        from src.vault_download import VaultLocalDiskFullError

        # Patch shutil.disk_usage in vault_restore to report 0 free bytes.
        import src.vault_restore as restore_mod
        original = restore_mod.shutil.disk_usage

        class _Stub:
            free = 0
            total = 1024
            used = 1024

        restore_mod.shutil.disk_usage = lambda _p: _Stub()  # type: ignore[assignment]
        try:
            vault = _vault()
            try:
                with self.assertRaises(VaultLocalDiskFullError):
                    restore_remote_folder(
                        vault=vault, relay=relay, manifest=manifest,
                        remote_folder_id=DOCS_ID, destination=self.dest,
                        device_name=DEVICE_NAME, when=WHEN,
                    )
            finally:
                vault.close()
        finally:
            restore_mod.shutil.disk_usage = original

    # ------------------------------------------------------------------
    # Unknown folder id raises
    # ------------------------------------------------------------------

    def test_unknown_remote_folder_raises_keyerror(self) -> None:
        relay, manifest = self._seed_remote({"x.txt": b"x"})
        vault = _vault()
        try:
            with self.assertRaises(KeyError):
                restore_remote_folder(
                    vault=vault, relay=relay, manifest=manifest,
                    remote_folder_id="rf_v1_z" * 5,
                    destination=self.dest,
                    device_name=DEVICE_NAME, when=WHEN,
                )
        finally:
            vault.close()

    # ------------------------------------------------------------------
    # Re-running a restore on a clean tree yields zero work
    # ------------------------------------------------------------------

    def test_second_restore_run_skips_everything_via_fingerprint(self) -> None:
        relay, manifest = self._seed_remote({"alpha.txt": b"unchanged"})
        vault = _vault()
        try:
            first = restore_remote_folder(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN,
            )
            second = restore_remote_folder(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN,
            )
        finally:
            vault.close()
        self.assertEqual(first.written, ["alpha.txt"])
        self.assertEqual(second.written, [])
        self.assertEqual(second.skipped_identical, ["alpha.txt"])


class RestoreAtDateTests(unittest.TestCase):
    """T11.5 — pick a date, find latest manifest revision ≤ date,
    materialize that snapshot at the chosen path with conflict copies.

    The fixtures build a manifest with two files versioned at distinct
    timestamps so each test can pick a cutoff and assert which version
    landed.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_restore_at_date_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.dest = self.tmpdir / "snapshot_target"
        self.dest.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_versions(self) -> tuple[FakeUploadRelay, dict, dict[str, str]]:
        """Build a remote with file alpha.txt at two versions + beta.txt at one.

        Timeline (UTC):

        - 2026-01-01: alpha v1 = b"alpha-v1"
        - 2026-02-01: beta  v1 = b"beta-v1"
        - 2026-03-01: alpha v2 = b"alpha-v2-current"
        """
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-01-01T00:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-01-01T00:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[],
                ),
            ],
        )
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            current = manifest
            payloads = {
                "alpha.txt-v1": b"alpha-v1",
                "beta.txt-v1": b"beta-v1",
                "alpha.txt-v2": b"alpha-v2-current",
            }
            for tag, ts, path, key in (
                ("alpha v1", "2026-01-01T12:00:00.000Z", "alpha.txt", "alpha.txt-v1"),
                ("beta v1", "2026-02-01T12:00:00.000Z", "beta.txt", "beta.txt-v1"),
                ("alpha v2", "2026-03-01T12:00:00.000Z", "alpha.txt", "alpha.txt-v2"),
            ):
                local = self.tmpdir / "src" / f"{key}"
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(payloads[key])
                res = upload_file(
                    vault=vault, relay=relay, manifest=current,
                    local_path=local, remote_folder_id=DOCS_ID,
                    remote_path=path, author_device_id=AUTHOR,
                    created_at=ts,
                )
                current = res.manifest
        finally:
            vault.close()
        from src.vault_browser_model import decrypt_manifest as _decrypt
        observer = _vault()
        try:
            published = _decrypt(observer, relay.current_envelope)
        finally:
            observer.close()
        return relay, published, payloads

    # ----------------------- Acceptance scenarios -----------------------

    def test_cutoff_after_v1_before_v2_writes_v1_bytes(self) -> None:
        """Restoring to a 2-week-old snapshot writes the snapshot bytes;
        current state on the relay is unchanged (never published)."""
        relay, manifest, payloads = self._seed_versions()
        published_count = len(relay.published_manifests)
        cutoff = datetime(2026, 2, 15, 0, 0, tzinfo=timezone.utc)

        vault = _vault()
        try:
            result = restore_remote_folder_at_date(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN, cutoff=cutoff,
            )
        finally:
            vault.close()

        # alpha.txt at the cutoff = v1 bytes; beta.txt = v1 bytes.
        self.assertEqual(set(result.written), {"alpha.txt", "beta.txt"})
        self.assertEqual(
            (self.dest / "alpha.txt").read_bytes(), payloads["alpha.txt-v1"],
        )
        self.assertEqual(
            (self.dest / "beta.txt").read_bytes(), payloads["beta.txt-v1"],
        )
        # Relay was never republished by the restore.
        self.assertEqual(len(relay.published_manifests), published_count)

    def test_cutoff_before_any_version_yields_empty_restore(self) -> None:
        relay, manifest, _ = self._seed_versions()
        cutoff = datetime(2025, 12, 1, tzinfo=timezone.utc)
        vault = _vault()
        try:
            result = restore_remote_folder_at_date(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN, cutoff=cutoff,
            )
        finally:
            vault.close()
        self.assertEqual(result.written, [])
        self.assertEqual(result.skipped_identical, [])
        self.assertEqual(result.conflict_copies, [])

    def test_cutoff_after_v2_writes_latest_version(self) -> None:
        relay, manifest, payloads = self._seed_versions()
        cutoff = datetime(2099, 1, 1, tzinfo=timezone.utc)
        vault = _vault()
        try:
            result = restore_remote_folder_at_date(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN, cutoff=cutoff,
            )
        finally:
            vault.close()
        self.assertEqual(
            (self.dest / "alpha.txt").read_bytes(), payloads["alpha.txt-v2"],
        )
        self.assertEqual(set(result.written), {"alpha.txt", "beta.txt"})

    def test_tombstoned_before_cutoff_is_skipped(self) -> None:
        # Build a remote with alpha.txt then tombstone it.
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-01-01T00:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-01-01T00:00:00.000Z",
                created_by_device_id=AUTHOR, entries=[],
            )],
        )
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            local = self.tmpdir / "src_alpha.txt"
            local.write_bytes(b"alpha bytes")
            res = upload_file(
                vault=vault, relay=relay, manifest=manifest,
                local_path=local, remote_folder_id=DOCS_ID,
                remote_path="alpha.txt", author_device_id=AUTHOR,
                created_at="2026-01-01T12:00:00.000Z",
            )
            current = tombstone_file_entry(
                res.manifest, remote_folder_id=DOCS_ID, path="alpha.txt",
                deleted_at="2026-02-01T12:00:00.000Z", author_device_id=AUTHOR,
            )
            current["revision"] = int(current["revision"]) + 1
            current["parent_revision"] = current["revision"] - 1
            vault.publish_manifest(relay, current)
        finally:
            vault.close()
        from src.vault_browser_model import decrypt_manifest as _decrypt
        observer = _vault()
        try:
            published = _decrypt(observer, relay.current_envelope)
        finally:
            observer.close()

        # Cutoff after tombstone → skipped (file was deleted at that point).
        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
        vault = _vault()
        try:
            result = restore_remote_folder_at_date(
                vault=vault, relay=relay, manifest=published,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN, cutoff=cutoff,
            )
        finally:
            vault.close()
        self.assertEqual(result.written, [])
        self.assertFalse((self.dest / "alpha.txt").exists())

        # Cutoff *before* tombstone → restore the live snapshot.
        shutil.rmtree(self.dest); self.dest.mkdir()
        cutoff_before = datetime(2026, 1, 15, tzinfo=timezone.utc)
        vault = _vault()
        try:
            result = restore_remote_folder_at_date(
                vault=vault, relay=relay, manifest=published,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN, cutoff=cutoff_before,
            )
        finally:
            vault.close()
        self.assertEqual(result.written, ["alpha.txt"])
        self.assertEqual((self.dest / "alpha.txt").read_bytes(), b"alpha bytes")

    def test_collision_in_destination_yields_a20_conflict_copy(self) -> None:
        relay, manifest, payloads = self._seed_versions()
        # Pre-place a file at alpha.txt so the snapshot collides.
        (self.dest / "alpha.txt").write_bytes(b"locally-edited")
        cutoff = datetime(2026, 2, 15, tzinfo=timezone.utc)
        vault = _vault()
        try:
            result = restore_remote_folder_at_date(
                vault=vault, relay=relay, manifest=manifest,
                remote_folder_id=DOCS_ID, destination=self.dest,
                device_name=DEVICE_NAME, when=WHEN, cutoff=cutoff,
            )
        finally:
            vault.close()
        self.assertEqual(
            (self.dest / "alpha.txt").read_bytes(), b"locally-edited",
        )
        self.assertEqual(len(result.conflict_copies), 1)
        original, conflict = result.conflict_copies[0]
        self.assertEqual(original, "alpha.txt")
        self.assertIn("conflict restored", conflict)
        self.assertEqual(
            (self.dest / conflict).read_bytes(), payloads["alpha.txt-v1"],
        )


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID, master_key=MASTER_KEY,
        recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1, manifest_revision=1,
        manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
    )


if __name__ == "__main__":
    unittest.main()
