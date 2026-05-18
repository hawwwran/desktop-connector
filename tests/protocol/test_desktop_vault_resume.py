"""Cross-session resume of an unfinished vault create — worker-thread tests.

Acceptance (temp/finished-plans/post-breakup-followups.md §2): a single onboarding session
leaves at most one ``vaults`` row on the relay regardless of how many
times the wizard was opened and closed. These tests exercise the worker
seam — the GTK wizard plumbing is a thin shell over
:func:`complete_pending_publish` / :func:`discard_pending_publish` /
the marker helpers.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

# Tests must not touch the host's keyring. Set BEFORE importing config —
# Config.__init__ reads the env var at construction.
os.environ.setdefault("DESKTOP_CONNECTOR_NO_KEYRING", "1")

from src.config import Config  # noqa: E402
from src.vault.crypto import (  # noqa: E402
    aead_decrypt,
    build_header_aad,
    derive_subkey,
)
from src.vault.grant.store import (  # noqa: E402
    FileGrantStore,
    VaultGrant,
    fallback_grant_path,
)
from src.vault.relay_errors import VaultNotFoundError  # noqa: E402
from src.vault.resume import (  # noqa: E402
    clear_pending_publish_marker,
    complete_pending_publish,
    discard_pending_publish,
    read_pending_publish_marker,
    set_pending_publish_marker,
)


VAULT_ID = "ABCD2345WXYZ"
DEVICE_SEED = b"\x55" * 32

# Reduced argon cost — keeps the test fast. The recovery_wrap_key
# derivation is intentionally memory-hard in production; we don't need
# that to verify shape.
ARGON_KIB = 8192
ARGON_ITERS = 2


# --------------------------------------------------------------------- helpers


class FakeResumeRelay:
    """In-memory relay sufficient for the resume-worker's surface.

    Records the latest header bytes for each vault id, supports
    ``create_vault`` (POST), ``put_header`` (CAS PUT), and ``get_header``
    (with a 404-shaped ``RuntimeError`` for unknown ids). Exposes
    counters so tests can assert "single publish landed".
    """

    def __init__(self) -> None:
        self.vaults: dict[str, dict] = {}
        self.create_calls = 0
        self.put_header_calls = 0
        self.get_header_calls = 0

    def create_vault(
        self,
        vault_id,
        vault_access_token_hash,
        encrypted_header,
        header_hash,
        initial_root_ciphertext,
        initial_root_hash,
        **kwargs,
    ) -> dict:
        self.create_calls += 1
        if vault_id in self.vaults:
            # Mirror the server's vault_already_exists 409 — the resume
            # path should never invoke this on an existing vault.
            raise RuntimeError(
                f"Relay rejected vault creation: HTTP 409 vault_already_exists"
            )
        self.vaults[vault_id] = {
            "encrypted_header": encrypted_header,
            "header_hash": header_hash,
            "header_revision": 1,
            "vault_access_token_hash": vault_access_token_hash,
            "root_envelope": initial_root_ciphertext,
            "root_hash": initial_root_hash,
            "root_revision": 1,
        }
        return {"vault_id": vault_id, "header_revision": 1}

    def get_header(self, vault_id: str, vault_access_secret: str) -> dict:
        self.get_header_calls += 1
        if vault_id not in self.vaults:
            # Mirror the production adapter, which raises the typed
            # error on HTTP 404 so the resume probe doesn't have to
            # substring-match an error message.
            raise VaultNotFoundError(f"orphan {vault_id} no longer on relay")
        row = self.vaults[vault_id]
        return {
            "encrypted_header": row["encrypted_header"],
            "header_hash": row["header_hash"],
            "header_revision": row["header_revision"],
            "quota_ciphertext_bytes": 0,
            "used_ciphertext_bytes": 0,
            "migrated_to": None,
        }

    def put_header(
        self,
        vault_id: str,
        vault_access_secret: str,
        *,
        expected_header_revision: int,
        new_header_revision: int,
        encrypted_header: bytes,
        header_hash: str,
    ) -> dict:
        self.put_header_calls += 1
        if vault_id not in self.vaults:
            raise RuntimeError(
                f"Relay rejected vault header replace: HTTP 404 vault_not_found"
            )
        row = self.vaults[vault_id]
        if row["header_revision"] != expected_header_revision:
            raise RuntimeError("Relay rejected vault header replace: HTTP 409 cas_conflict")
        row["encrypted_header"] = encrypted_header
        row["header_hash"] = header_hash
        row["header_revision"] = new_header_revision
        return {"header_revision": new_header_revision, "header_hash": header_hash}


def _seed_grant(
    config_dir: Path,
    master_key: bytes,
    vault_access_secret: str,
    vault_id: str = VAULT_ID,
) -> None:
    """Drop a usable grant into the file backend at config_dir.

    With ``DESKTOP_CONNECTOR_NO_KEYRING=1`` the production loader still
    probes the keyring first (and finds nothing), then falls back to the
    file backend keyed off the device seed — KeyManager's private key.
    For these tests we sidestep KeyManager by writing directly with a
    fixed seed and supplying the same seed in ``grant_loader``.
    """
    store = FileGrantStore(config_dir=config_dir, device_seed=DEVICE_SEED)
    grant = VaultGrant.from_bytes(vault_id, master_key, vault_access_secret)
    try:
        store.save(grant)
    finally:
        grant.zero()


def _file_grant_loader(config_dir: Path, vault_id: str = VAULT_ID):
    """Mirror :func:`open_local_vault_from_grant` against the file
    backend with a fixed device seed, so the test doesn't need a real
    KeyManager / OS keyring.
    """
    def loader():
        store = FileGrantStore(config_dir=config_dir, device_seed=DEVICE_SEED)
        grant = store.load(vault_id)
        if grant is None:
            raise RuntimeError("no local grant for this vault")
        try:
            return bytes(grant.master_key), grant.vault_access_secret
        finally:
            grant.zero()
    return loader


# --------------------------------------------------------------------- marker round-trip


class PendingPublishMarkerTests(unittest.TestCase):
    def test_complete_pending_publish_seeks_by_plaintext_size(self) -> None:
        """Review §4.H5: the upload-resume's seek-past-completed-chunk
        path used ``int(session.chunk_size)`` — fine for full chunks
        but overshoots EOF on the last chunk of any file whose size
        isn't a multiple of CHUNK_SIZE. Currently raises (no silent
        corruption) but blocks legitimate last-chunk resume. The fix
        uses the record's stored ``plaintext_size``.

        Source-level pin: if a future refactor reverts the constant
        to ``session.chunk_size`` this test fails."""
        from pathlib import Path as _P
        from tests.protocol._paths import REPO_ROOT
        source = (
            _P(REPO_ROOT)
            / "desktop"
            / "src"
            / "vault"
            / "upload"
            / "resume.py"
        ).read_text()
        # The seek/read path reads the per-record plaintext_size.
        self.assertIn(
            'plaintext_size = int(record.get("plaintext_size"', source,
            "resume seek/read must consult per-record plaintext_size",
        )
        # And does NOT seek by session.chunk_size in the skip branch.
        self.assertNotIn(
            'fh.seek(int(session.chunk_size), os.SEEK_CUR)', source,
            "resume seek must not use session.chunk_size (overshoots last chunk)",
        )

    def test_set_then_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp))
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
                now_provider=lambda: "2026-05-12T12:00:00Z",
            )
            marker = read_pending_publish_marker(config)
            self.assertIsNotNone(marker)
            self.assertEqual(marker["vault_id"], VAULT_ID)
            self.assertEqual(marker["server_url"], "http://example.test/relay")
            self.assertEqual(marker["created_at"], "2026-05-12T12:00:00Z")

    def test_set_writes_to_disk_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp))
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )
            reopened = Config(Path(tmp))
            marker = read_pending_publish_marker(reopened)
            self.assertIsNotNone(marker)
            self.assertEqual(marker["vault_id"], VAULT_ID)

    def test_clear_removes_marker_without_disturbing_other_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp))
            config._data["vault"] = {"active": True}
            config.save()
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )
            clear_pending_publish_marker(config)
            config.save()
            reopened = Config(Path(tmp))
            self.assertIsNone(read_pending_publish_marker(reopened))
            self.assertTrue(reopened.vault_active)

    def test_read_returns_none_when_no_vault_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp))
            self.assertIsNone(read_pending_publish_marker(config))

    def test_read_returns_none_for_malformed_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp))
            config._data["vault"] = {"pending_publish": "not-a-dict"}
            self.assertIsNone(read_pending_publish_marker(config))


# --------------------------------------------------------------------- discard


class DiscardPendingPublishTests(unittest.TestCase):
    def test_discard_removes_grant_and_clears_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(tmp_path)
            _seed_grant(tmp_path, b"\x42" * 32, "bearer-secret")
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )
            self.assertTrue(fallback_grant_path(tmp_path, VAULT_ID).exists())

            discard_pending_publish(tmp_path, config, VAULT_ID)

            self.assertFalse(fallback_grant_path(tmp_path, VAULT_ID).exists())
            reopened = Config(tmp_path)
            self.assertIsNone(read_pending_publish_marker(reopened))

    def test_discard_is_idempotent_when_grant_already_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(tmp_path)
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )
            # No grant on disk — discard should still clear the marker.
            discard_pending_publish(tmp_path, config, VAULT_ID)

            reopened = Config(tmp_path)
            self.assertIsNone(read_pending_publish_marker(reopened))


# --------------------------------------------------------------------- complete


class CompletePendingPublishTests(unittest.TestCase):
    PASSPHRASE = "correct horse battery staple"

    def _seed_orphan_on_relay(
        self,
        relay: FakeResumeRelay,
        vault_id: str = VAULT_ID,
        master_key: bytes = b"\x42" * 32,
        vault_access_secret: str = "bearer-secret",
    ) -> None:
        """Plant an entry shaped like what a prior session's
        ``Vault.prepare_new`` + ``publish_initial`` would have left.
        The body is not byte-identical to a real publish (we don't
        care for the resume worker), but the size and keys are
        plausible — get_header just returns whatever we stashed.
        """
        relay.vaults[vault_id] = {
            "encrypted_header": b"\x01" + vault_id.encode("ascii") + b"\x00" * 64,
            "header_hash": "0" * 64,
            "header_revision": 1,
            "vault_access_token_hash": b"\x00" * 32,
            "manifest_envelope_bytes": b"",
            "manifest_hash": "",
            "manifest_revision": 1,
        }

    def test_adopt_path_replaces_header_in_place(self) -> None:
        """Relay already has the row: complete_pending_publish PUTs a
        new header at revision+1, writes last_known_id to config,
        clears the marker, and returns a state suitable for
        Export+Verify."""
        master_key = b"\x42" * 32
        vault_access_secret = "bearer-secret"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(tmp_path)
            _seed_grant(tmp_path, master_key, vault_access_secret)
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )

            relay = FakeResumeRelay()
            self._seed_orphan_on_relay(relay)

            resumed = complete_pending_publish(
                tmp_path, config, VAULT_ID, self.PASSPHRASE,
                relay=relay,
                grant_loader=_file_grant_loader(tmp_path),
                argon_memory_kib=ARGON_KIB,
                argon_iterations=ARGON_ITERS,
            )

            self.assertEqual(relay.create_calls, 0)
            self.assertEqual(relay.put_header_calls, 1)
            self.assertEqual(relay.vaults[VAULT_ID]["header_revision"], 2)

            # config.json has the canonical id + recovery envelope meta;
            # marker is cleared (atomic).
            reopened = Config(tmp_path)
            self.assertEqual(
                reopened._data["vault"]["last_known_id"], VAULT_ID,
            )
            self.assertIn("recovery_envelope_meta", reopened._data["vault"])
            self.assertIsNone(read_pending_publish_marker(reopened))

            # Returned state covers the success-screen surface — the
            # wizard pipes these straight into state[].
            self.assertEqual(resumed.vault_id, VAULT_ID)
            self.assertEqual(resumed.vault_access_secret, vault_access_secret)
            self.assertEqual(len(resumed.recovery_secret_bytes), 32)
            self.assertEqual(resumed.recovery_envelope_meta["argon_memory_kib"], ARGON_KIB)
            self.assertEqual(resumed.recovery_envelope_meta["argon_iterations"], ARGON_ITERS)

    def test_adopt_path_new_header_decrypts_under_master_key(self) -> None:
        """The header we PUT must remain decryptable by the same
        master key the prior session used — adopt rewrites the
        recovery envelope but keeps the data-plane key stable.
        """
        master_key = b"\x42" * 32
        vault_access_secret = "bearer-secret"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(tmp_path)
            _seed_grant(tmp_path, master_key, vault_access_secret)
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )

            relay = FakeResumeRelay()
            self._seed_orphan_on_relay(relay)

            complete_pending_publish(
                tmp_path, config, VAULT_ID, self.PASSPHRASE,
                relay=relay,
                grant_loader=_file_grant_loader(tmp_path),
                argon_memory_kib=ARGON_KIB,
                argon_iterations=ARGON_ITERS,
            )

            # The new header envelope is at revision 2. Parse it and
            # decrypt with k_header derived from the same master key.
            envelope = relay.vaults[VAULT_ID]["encrypted_header"]
            self.assertGreater(len(envelope), 1 + 12 + 8 + 24 + 16)
            nonce = envelope[1 + 12 + 8 : 1 + 12 + 8 + 24]
            ct = envelope[1 + 12 + 8 + 24:]
            header_subkey = derive_subkey("dc-vault-v1/header", master_key)
            plaintext = aead_decrypt(
                ct, header_subkey, nonce,
                build_header_aad(VAULT_ID, header_revision=2),
            )
            self.assertIn(b"dc-vault-header-v1", plaintext)
            self.assertIn(VAULT_ID.encode("ascii"), plaintext)

    def test_publish_path_creates_vault_when_relay_404s(self) -> None:
        """Relay doesn't have the row — complete_pending_publish POSTs
        a full create with the existing master key + a fresh recovery
        envelope + a fresh genesis manifest. No PUT-header call."""
        master_key = b"\x42" * 32
        vault_access_secret = "bearer-secret"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(tmp_path)
            _seed_grant(tmp_path, master_key, vault_access_secret)
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )

            relay = FakeResumeRelay()  # no orphan seeded

            resumed = complete_pending_publish(
                tmp_path, config, VAULT_ID, self.PASSPHRASE,
                relay=relay,
                grant_loader=_file_grant_loader(tmp_path),
                argon_memory_kib=ARGON_KIB,
                argon_iterations=ARGON_ITERS,
            )

            self.assertEqual(relay.create_calls, 1)
            self.assertEqual(relay.put_header_calls, 0)
            self.assertIn(VAULT_ID, relay.vaults)
            self.assertEqual(relay.vaults[VAULT_ID]["header_revision"], 1)

            reopened = Config(tmp_path)
            self.assertEqual(
                reopened._data["vault"]["last_known_id"], VAULT_ID,
            )
            self.assertIsNone(read_pending_publish_marker(reopened))
            self.assertEqual(resumed.vault_id, VAULT_ID)

    def test_marker_survives_publish_failure(self) -> None:
        """If the relay POST raises, last_known_id stays unset and the
        marker stays in config — the user can re-launch and try again.
        """
        master_key = b"\x42" * 32
        vault_access_secret = "bearer-secret"

        class FailingRelay(FakeResumeRelay):
            def create_vault(self, *args, **kwargs):
                raise RuntimeError("Relay rejected vault creation: HTTP 500")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(tmp_path)
            _seed_grant(tmp_path, master_key, vault_access_secret)
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )

            relay = FailingRelay()

            with self.assertRaises(RuntimeError):
                complete_pending_publish(
                    tmp_path, config, VAULT_ID, self.PASSPHRASE,
                    relay=relay,
                    grant_loader=_file_grant_loader(tmp_path),
                    argon_memory_kib=ARGON_KIB,
                    argon_iterations=ARGON_ITERS,
                )

            reopened = Config(tmp_path)
            self.assertNotIn(
                "last_known_id", reopened._data.get("vault", {}),
            )
            marker = read_pending_publish_marker(reopened)
            self.assertIsNotNone(marker)
            self.assertEqual(marker["vault_id"], VAULT_ID)

    def test_missing_grant_raises_cleanly(self) -> None:
        """If the local grant was deleted out of band between sessions
        (manual keyring purge, OS-keyring backend swap, half-finished
        cleanup), Resume cannot proceed. The worker must surface that
        as a clean error rather than crash partway through publishing —
        the wizard's failure-handler then routes back to Discard.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(tmp_path)
            # Marker present but no grant on disk.
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )

            def empty_loader():
                raise RuntimeError("no local grant for this vault")

            relay = FakeResumeRelay()
            with self.assertRaises(RuntimeError):
                complete_pending_publish(
                    tmp_path, config, VAULT_ID, self.PASSPHRASE,
                    relay=relay,
                    grant_loader=empty_loader,
                    argon_memory_kib=ARGON_KIB,
                    argon_iterations=ARGON_ITERS,
                )

            # No relay calls, no config mutation, marker still present
            # so the user can retry or Discard.
            self.assertEqual(relay.get_header_calls, 0)
            self.assertEqual(relay.create_calls, 0)
            self.assertEqual(relay.put_header_calls, 0)
            reopened = Config(tmp_path)
            self.assertNotIn(
                "last_known_id", reopened._data.get("vault", {}),
            )
            self.assertIsNotNone(read_pending_publish_marker(reopened))

    def test_repeated_resume_against_same_orphan_is_safe(self) -> None:
        """Acceptance: a single onboarding session leaves at most one
        ``vaults`` row regardless of how many times the wizard was
        opened. Two back-to-back Resume runs against the same orphan
        produce one row on the relay (PUT-header path on the second
        run; the first POST'd via the 404 path, the second sees the
        row and re-PUTs).
        """
        master_key = b"\x42" * 32
        vault_access_secret = "bearer-secret"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(tmp_path)
            _seed_grant(tmp_path, master_key, vault_access_secret)
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )

            relay = FakeResumeRelay()  # starts with no rows

            # First Resume — relay 404s → POST.
            complete_pending_publish(
                tmp_path, config, VAULT_ID, self.PASSPHRASE,
                relay=relay,
                grant_loader=_file_grant_loader(tmp_path),
                argon_memory_kib=ARGON_KIB,
                argon_iterations=ARGON_ITERS,
            )
            self.assertEqual(len(relay.vaults), 1)

            # Re-arm marker (a real user would do this by closing the
            # success screen without clicking Done; the wizard's
            # commit-failure path is the natural trigger). Then resume
            # again.
            set_pending_publish_marker(
                config, VAULT_ID, "http://example.test/relay",
            )
            complete_pending_publish(
                tmp_path, config, VAULT_ID, self.PASSPHRASE,
                relay=relay,
                grant_loader=_file_grant_loader(tmp_path),
                argon_memory_kib=ARGON_KIB,
                argon_iterations=ARGON_ITERS,
            )

            self.assertEqual(len(relay.vaults), 1)
            self.assertEqual(relay.create_calls, 1)
            self.assertEqual(relay.put_header_calls, 1)


if __name__ == "__main__":
    unittest.main()
