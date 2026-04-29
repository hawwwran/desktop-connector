"""Tests for hardening-plan H.7 — KeyManager + secret-store.

Pin the post-H.7 contract:

  - When a secure store is provided, the long-term private key
    lives there exclusively (no PEM file on disk after migration).
  - When no secure store is provided (legacy callers, JSON
    fallback, no-keyring deployments), the PEM file is the
    storage of record — pre-H.7 behaviour byte-for-byte.
  - Migration is one-shot, idempotent across re-inits, and safe
    on partial failure (PEM stays put, retried next boot).
  - ``reset_keys`` wipes from BOTH backends so the next load
    can't pick up stale state.
  - ``scrub_private_key`` covers the long-running-process /
    Settings-Verify scenario where a PEM appears after the
    init-time migration has already run.

Tests use the same in-memory fake keyring module the H.3 tests
use, so no real OS keyring is touched.
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

from cryptography.hazmat.primitives import serialization  # noqa: E402

from src.crypto import KeyManager, PRIVATE_KEY_FILENAME  # noqa: E402
from src.secrets import (  # noqa: E402
    SECRET_KEY_PRIVATE_KEY_PEM,
    SERVICE_NAME,
    JsonFallbackStore,
    SecretServiceStore,
    SecretServiceUnavailable,
)


def _serialize_pem(km: KeyManager) -> bytes:
    return km.private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class _FakeKeyringErrors:
    class PasswordDeleteError(Exception):
        pass

    class KeyringError(Exception):
        pass


class _FakeKeyring:
    """Trimmed down stand-in matching the test_desktop_secrets.py
    fake. Duplicated here to keep the keymanager tests self-contained."""

    errors = _FakeKeyringErrors

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_set_with: Exception | None = None
        self.fail_get_with: Exception | None = None
        self.fail_delete_with: Exception | None = None

    def get_password(self, service: str, username: str) -> str | None:
        if self.fail_get_with is not None:
            raise self.fail_get_with
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_set_with is not None:
            raise self.fail_set_with
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.fail_delete_with is not None:
            raise self.fail_delete_with
        if (service, username) not in self.values:
            raise self.errors.PasswordDeleteError("no such entry")
        del self.values[(service, username)]


def _pem_path(config_dir: Path) -> Path:
    return config_dir / "keys" / PRIVATE_KEY_FILENAME


def _new_secure_store() -> tuple[SecretServiceStore, _FakeKeyring]:
    fake = _FakeKeyring()
    return SecretServiceStore(keyring_module=fake), fake


# --- Pre-H.7 backward compatibility (no secret_store passed) ---------


class LegacyPemOnlyTests(unittest.TestCase):
    """Pin the pre-H.7 behaviour: when no secret_store is passed,
    KeyManager reads/writes the PEM file as it always did. Important
    so existing tests + AppImage migration tests keep working."""

    def test_fresh_install_writes_pem(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            km = KeyManager(config_dir)
            self.assertTrue(_pem_path(config_dir).exists())
            self.assertEqual(
                _pem_path(config_dir).stat().st_mode & 0o777,
                0o600,
            )
            self.assertIsNotNone(km.private_key)
            self.assertFalse(km.was_pem_migrated)

    def test_existing_pem_loaded_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            # First instance creates the PEM
            km1 = KeyManager(config_dir)
            pubkey_b1 = km1.get_public_key_bytes()
            # Second instance reuses it — public key must be identical
            km2 = KeyManager(config_dir)
            self.assertEqual(km2.get_public_key_bytes(), pubkey_b1)

    def test_reset_keys_regenerates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            km = KeyManager(config_dir)
            old_pub = km.get_public_key_bytes()
            km.reset_keys()
            self.assertNotEqual(km.get_public_key_bytes(), old_pub)
            self.assertTrue(_pem_path(config_dir).exists())


# --- Secure store path: store-of-record is the keyring ---------------


class SecureStorePathTests(unittest.TestCase):
    def test_fresh_install_writes_to_keyring_no_pem(self) -> None:
        store, fake = _new_secure_store()
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            km = KeyManager(config_dir, secret_store=store)
            self.assertIn(
                (SERVICE_NAME, SECRET_KEY_PRIVATE_KEY_PEM),
                fake.values,
                "fresh install should write the private key to the keyring",
            )
            self.assertFalse(
                _pem_path(config_dir).exists(),
                "no PEM file should land on disk when the keyring is reachable",
            )
            self.assertIsNotNone(km.private_key)
            self.assertFalse(
                km.was_pem_migrated,
                "fresh install isn't a migration",
            )

    def test_round_trip_across_reinits_via_keyring(self) -> None:
        store, fake = _new_secure_store()
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            km1 = KeyManager(config_dir, secret_store=store)
            pub1 = km1.get_public_key_bytes()
            # Re-instantiate with the SAME fake keyring backend.
            store2 = SecretServiceStore(keyring_module=fake)
            km2 = KeyManager(config_dir, secret_store=store2)
            self.assertEqual(km2.get_public_key_bytes(), pub1)
            self.assertFalse(_pem_path(config_dir).exists())

    def test_reset_keys_wipes_both_backends(self) -> None:
        store, fake = _new_secure_store()
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            km = KeyManager(config_dir, secret_store=store)
            old_pub = km.get_public_key_bytes()
            # Sanity: keyring entry present, no PEM
            self.assertIn(
                (SERVICE_NAME, SECRET_KEY_PRIVATE_KEY_PEM), fake.values,
            )
            km.reset_keys()
            # New identity
            self.assertNotEqual(km.get_public_key_bytes(), old_pub)
            # Keyring still has exactly one entry — the new one,
            # not a stale leftover next to the new one.
            entries = [
                k for k in fake.values
                if k[1] == SECRET_KEY_PRIVATE_KEY_PEM
            ]
            self.assertEqual(len(entries), 1)
            # Disk path stayed clean.
            self.assertFalse(_pem_path(config_dir).exists())


# --- Migration of a pre-H.7 PEM into the keyring ---------------------


def _seed_legacy_pem(config_dir: Path) -> bytes:
    """Generate a PEM and write it where pre-H.7 KeyManager would.
    Returns the PEM bytes for downstream comparison."""
    legacy_km = KeyManager(config_dir)  # no secret_store → PEM-only path
    return _pem_path(config_dir).read_bytes()


class MigrationTests(unittest.TestCase):
    def test_existing_pem_migrates_into_keyring_on_first_boot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            legacy_pem = _seed_legacy_pem(config_dir)
            self.assertTrue(_pem_path(config_dir).exists())

            store, fake = _new_secure_store()
            km = KeyManager(config_dir, secret_store=store)

            # PEM moved into the keyring under the canonical key
            self.assertEqual(
                fake.values[(SERVICE_NAME, SECRET_KEY_PRIVATE_KEY_PEM)],
                legacy_pem.decode("ascii"),
            )
            # PEM file removed from disk
            self.assertFalse(_pem_path(config_dir).exists())
            # Migration signal set for this init
            self.assertTrue(km.was_pem_migrated)

    def test_migration_preserves_identity(self) -> None:
        # Sanity: the migration MUST not change the public key —
        # paired phones would lose their pairing otherwise.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            legacy = KeyManager(config_dir)
            legacy_pub = legacy.get_public_key_bytes()
            del legacy

            store, _fake = _new_secure_store()
            migrated = KeyManager(config_dir, secret_store=store)
            self.assertEqual(migrated.get_public_key_bytes(), legacy_pub)

    def test_migration_idempotent_across_reinits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_pem(config_dir)
            store, fake = _new_secure_store()

            km1 = KeyManager(config_dir, secret_store=store)
            self.assertTrue(km1.was_pem_migrated)

            # Second init shouldn't re-migrate — keyring already
            # authoritative, no PEM on disk.
            store2 = SecretServiceStore(keyring_module=fake)
            km2 = KeyManager(config_dir, secret_store=store2)
            self.assertFalse(
                km2.was_pem_migrated,
                "a second boot must not re-trigger the migration signal",
            )
            self.assertEqual(
                km1.get_public_key_bytes(),
                km2.get_public_key_bytes(),
            )

    def test_migration_partial_failure_leaves_pem_in_place(self) -> None:
        # set() raises mid-migration. KeyManager keeps the in-memory
        # key (loaded from the PEM), leaves the file on disk, lets
        # the next boot retry.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_pem(config_dir)
            store, fake = _new_secure_store()
            fake.fail_set_with = fake.errors.KeyringError(
                "simulated mid-migration drop",
            )
            km = KeyManager(config_dir, secret_store=store)
            # Key loaded from PEM, but not stored in keyring
            self.assertIsNotNone(km.private_key)
            self.assertNotIn(
                (SERVICE_NAME, SECRET_KEY_PRIVATE_KEY_PEM), fake.values,
            )
            # PEM left intact for retry
            self.assertTrue(_pem_path(config_dir).exists())
            self.assertFalse(km.was_pem_migrated)

    def test_stale_pem_is_cleaned_up_when_keyring_authoritative(self) -> None:
        # Scenario: a previous boot migrated and deleted the PEM,
        # then the operator restored the file from a backup, then
        # restarted. The keyring still has the (same) key. KeyManager
        # should detect the stale PEM and remove it again.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_pem(config_dir)
            store, fake = _new_secure_store()
            KeyManager(config_dir, secret_store=store)  # migrates
            self.assertFalse(_pem_path(config_dir).exists())
            # Pretend the operator restored the PEM (same content
            # as what's in the keyring — common after a backup).
            _pem_path(config_dir).write_bytes(
                fake.values[
                    (SERVICE_NAME, SECRET_KEY_PRIVATE_KEY_PEM)
                ].encode("ascii"),
            )
            self.assertTrue(_pem_path(config_dir).exists())

            store2 = SecretServiceStore(keyring_module=fake)
            km = KeyManager(config_dir, secret_store=store2)
            # KeyManager picked the keyring; cleaned up the PEM.
            self.assertFalse(_pem_path(config_dir).exists())
            self.assertFalse(km.was_pem_migrated)


# --- Insecure store: PEM remains the storage of record ---------------


class InsecureStoreTests(unittest.TestCase):
    """When the active store is the JSON fallback (no keyring
    reachable), KeyManager must NOT touch the keyring side at all.
    Existing PEMs stay; fresh installs write a PEM."""

    def test_insecure_store_uses_pem_only(self) -> None:
        json_store = JsonFallbackStore({}, lambda: None)
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            km = KeyManager(config_dir, secret_store=json_store)
            # PEM written, no keyring side state involved
            self.assertTrue(_pem_path(config_dir).exists())
            self.assertIsNotNone(km.private_key)

    def test_insecure_store_does_not_migrate_existing_pem(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_pem(config_dir)
            json_store = JsonFallbackStore({}, lambda: None)
            km = KeyManager(config_dir, secret_store=json_store)
            # PEM still there — insecure store can't migrate to itself
            self.assertTrue(_pem_path(config_dir).exists())
            self.assertFalse(km.was_pem_migrated)


# --- scrub_private_key (Settings Verify / --scrub-secrets path) ------


class ScrubPrivateKeyTests(unittest.TestCase):
    def test_no_op_after_init_already_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_pem(config_dir)
            store, _fake = _new_secure_store()
            km = KeyManager(config_dir, secret_store=store)
            # init migrated already; scrub finds nothing.
            self.assertFalse(km.scrub_private_key())

    def test_picks_up_pem_appearing_after_init(self) -> None:
        # Settings Verify use case: PEM was placed (e.g. KeePassXC
        # restore) after KeyManager already finished its init-time
        # migration check.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            store, fake = _new_secure_store()
            # First: fresh install into keyring (no PEM ever).
            km = KeyManager(config_dir, secret_store=store)
            self.assertFalse(_pem_path(config_dir).exists())
            # Operator drops a PEM next to the running process —
            # different content from what's in the keyring.
            other_pem = _serialize_pem(KeyManager(Path(tempfile.mkdtemp())))
            _pem_path(config_dir).parent.mkdir(parents=True, exist_ok=True)
            _pem_path(config_dir).write_bytes(other_pem)
            # Keyring already authoritative — scrub should detect
            # that and clean up the stale PEM, NOT migrate it.
            scrubbed = km.scrub_private_key()
            self.assertFalse(scrubbed)  # not a migration; was a stale-removal
            self.assertFalse(_pem_path(config_dir).exists())

    def test_picks_up_pem_when_keyring_was_empty(self) -> None:
        # Edge case: somehow the keyring entry vanished but a PEM
        # exists. scrub should migrate the PEM.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            store, fake = _new_secure_store()
            km = KeyManager(config_dir, secret_store=store)
            # Wipe the keyring entry behind KeyManager's back.
            del fake.values[(SERVICE_NAME, SECRET_KEY_PRIVATE_KEY_PEM)]
            # Drop a PEM file with the (in-memory) key bytes.
            _pem_path(config_dir).parent.mkdir(parents=True, exist_ok=True)
            _pem_path(config_dir).write_bytes(_serialize_pem(km))
            # scrub should re-populate the keyring AND remove the PEM
            self.assertTrue(km.scrub_private_key())
            self.assertIn(
                (SERVICE_NAME, SECRET_KEY_PRIVATE_KEY_PEM), fake.values,
            )
            self.assertFalse(_pem_path(config_dir).exists())

    def test_no_op_with_insecure_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_pem(config_dir)
            json_store = JsonFallbackStore({}, lambda: None)
            km = KeyManager(config_dir, secret_store=json_store)
            self.assertFalse(km.scrub_private_key())
            self.assertTrue(_pem_path(config_dir).exists())

    def test_no_op_when_no_store_provided(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            km = KeyManager(config_dir)  # no store
            self.assertFalse(km.scrub_private_key())


# --- Failure modes during normal operation ---------------------------


class StoreFailureTests(unittest.TestCase):
    def test_store_read_failure_falls_back_to_pem(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_pem(config_dir)
            store, fake = _new_secure_store()
            # Simulate the keyring failing the very first probe
            # done by KeyManager (after the SecretServiceStore probe
            # in __init__ already passed). __init__'s probe uses
            # username "_probe", whereas KeyManager's first call is
            # for SECRET_KEY_PRIVATE_KEY_PEM — so we install the
            # failure AFTER probe-time.
            fake.fail_get_with = fake.errors.KeyringError(
                "mid-session lock",
            )
            km = KeyManager(config_dir, secret_store=store)
            # Falls back to PEM — desktop still functional
            self.assertIsNotNone(km.private_key)
            self.assertTrue(_pem_path(config_dir).exists())

    def test_corrupt_keyring_entry_raises_rather_than_regenerating(self) -> None:
        # Anti-pattern guard: if the keyring entry is corrupt,
        # silently regenerating would leak the user's identity.
        # KeyManager must surface the corruption to the operator.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            store, fake = _new_secure_store()
            # Pre-seed the keyring with garbage that won't parse.
            fake.values[
                (SERVICE_NAME, SECRET_KEY_PRIVATE_KEY_PEM)
            ] = "this is not a PEM"
            with self.assertRaises((ValueError, TypeError)):
                KeyManager(config_dir, secret_store=store)


if __name__ == "__main__":
    unittest.main()
