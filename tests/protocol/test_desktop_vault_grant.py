"""T3.2 — Vault grant storage tests (keyring + file fallback).

Acceptance: both code paths exercised; sensitive material zeroed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_grant import (  # noqa: E402
    FileGrantStore,
    KeyringGrantStore,
    KeyringUnavailable,
    VaultGrant,
    delete_local_grant_artifacts,
    fallback_grant_path,
    open_default_grant_store,
)


VAULT_ID = "ABCD2345WXYZ"


class FakeKeyring:
    """In-memory keyring — drop-in for the ``keyring`` module's
    ``get_password`` / ``set_password`` / ``delete_password`` surface.
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str):
        return self.store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.store[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        self.store.pop((service, key), None)


class VaultGrantTests(unittest.TestCase):
    def test_zero_overwrites_master_key(self) -> None:
        grant = VaultGrant.from_bytes(VAULT_ID, b"\xff" * 32, "secret-bearer")
        self.assertEqual(grant.master_key, b"\xff" * 32)

        grant.zero()
        self.assertEqual(grant.master_key, b"")
        self.assertEqual(grant.vault_access_secret, "")

    def test_round_trip_via_json(self) -> None:
        original = VaultGrant.from_bytes(VAULT_ID, b"\x42" * 32, "bearer-string")
        decoded = VaultGrant.from_json(original.to_json())
        self.assertEqual(decoded.vault_id, VAULT_ID)
        self.assertEqual(decoded.master_key, b"\x42" * 32)
        self.assertEqual(decoded.vault_access_secret, "bearer-string")

    def test_rejects_wrong_master_key_length(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be 32 bytes"):
            VaultGrant.from_bytes(VAULT_ID, b"too-short", "x")


class KeyringGrantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeKeyring()
        self.store = KeyringGrantStore(self.fake)
        self.grant = VaultGrant.from_bytes(VAULT_ID, b"\x11" * 32, "kr-bearer")

    def test_save_then_load(self) -> None:
        self.store.save(self.grant)
        loaded = self.store.load(VAULT_ID)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.vault_id, VAULT_ID)
        self.assertEqual(loaded.master_key, b"\x11" * 32)
        self.assertEqual(loaded.vault_access_secret, "kr-bearer")

    def test_load_unknown_returns_none(self) -> None:
        self.assertIsNone(self.store.load("UNKNOWNVAULT"))

    def test_delete_then_load_returns_none(self) -> None:
        self.store.save(self.grant)
        self.store.delete(VAULT_ID)
        self.assertIsNone(self.store.load(VAULT_ID))

    def test_delete_unknown_is_idempotent(self) -> None:
        self.store.delete("NONEXISTENT")

    def test_keyring_uses_canonical_service_name(self) -> None:
        self.store.save(self.grant)
        keys = list(self.fake.store.keys())
        self.assertEqual(len(keys), 1)
        service, key = keys[0]
        self.assertEqual(service, "desktop-connector")
        self.assertTrue(key.startswith("vault_grant:"))


class FileGrantStoreTests(unittest.TestCase):
    SEED = b"\x55" * 32

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="vault_grant_test_")
        self.store = FileGrantStore(config_dir=self.tmpdir, device_seed=self.SEED)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_writes_aead_encrypted_envelope(self) -> None:
        grant = VaultGrant.from_bytes(VAULT_ID, b"\x77" * 32, "file-bearer")
        self.store.save(grant)

        path = os.path.join(self.tmpdir, f"vault_grant_{VAULT_ID}.json")
        self.assertTrue(os.path.exists(path))
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

        with open(path, "rb") as f:
            on_disk = f.read()
        self.assertNotIn(b"file-bearer", on_disk)
        self.assertNotIn(b"\x77" * 32, on_disk)

    def test_round_trip(self) -> None:
        grant = VaultGrant.from_bytes(VAULT_ID, b"\x77" * 32, "file-bearer")
        self.store.save(grant)
        loaded = self.store.load(VAULT_ID)
        self.assertEqual(loaded.master_key, b"\x77" * 32)
        self.assertEqual(loaded.vault_access_secret, "file-bearer")

    def test_load_unknown_returns_none(self) -> None:
        self.assertIsNone(self.store.load("UNKNOWNVAULT"))

    def test_delete_then_load_returns_none(self) -> None:
        grant = VaultGrant.from_bytes(VAULT_ID, b"\x77" * 32, "x")
        self.store.save(grant)
        self.store.delete(VAULT_ID)
        self.assertIsNone(self.store.load(VAULT_ID))

    def test_delete_unknown_is_idempotent(self) -> None:
        self.store.delete("NEVER")

    def test_different_seed_cannot_decrypt(self) -> None:
        grant = VaultGrant.from_bytes(VAULT_ID, b"\x77" * 32, "x")
        self.store.save(grant)
        other = FileGrantStore(config_dir=self.tmpdir, device_seed=b"\xee" * 32)
        from nacl.exceptions import CryptoError
        with self.assertRaises(CryptoError):
            other.load(VAULT_ID)

    def test_zero_wrap_key_overwrites(self) -> None:
        self.store.zero_wrap_key()
        self.assertNotIn(b"\x55", self.store._wrap_key)

    def test_rejects_short_seed(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 16 bytes"):
            FileGrantStore(config_dir=self.tmpdir, device_seed=b"too-short")


class OpenDefaultGrantStoreTests(unittest.TestCase):
    def test_falls_back_to_file_when_keyring_unavailable(self) -> None:
        import src.vault_grant as vault_grant
        original = vault_grant.KeyringGrantStore.open_default
        vault_grant.KeyringGrantStore.open_default = classmethod(
            lambda cls: (_ for _ in ()).throw(KeyringUnavailable("test forced"))
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = open_default_grant_store(
                    config_dir=tmp,
                    device_seed_provider=lambda: b"\x33" * 32,
                )
                self.assertIsInstance(store, FileGrantStore)
        finally:
            vault_grant.KeyringGrantStore.open_default = original


class DeleteLocalGrantArtifactsTests(unittest.TestCase):
    def test_removes_file_fallback_without_device_seed(self) -> None:
        import src.vault_grant as vault_grant
        original = vault_grant.KeyringGrantStore.open_default
        vault_grant.KeyringGrantStore.open_default = classmethod(
            lambda cls: (_ for _ in ()).throw(KeyringUnavailable("test forced"))
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = fallback_grant_path(tmp, VAULT_ID)
                path.write_text("encrypted envelope", encoding="utf-8")
                self.assertTrue(path.exists())

                delete_local_grant_artifacts(tmp, VAULT_ID)

                self.assertFalse(path.exists())
        finally:
            vault_grant.KeyringGrantStore.open_default = original

    def test_removes_keyring_grant_when_keyring_available(self) -> None:
        import src.vault_grant as vault_grant
        fake = FakeKeyring()
        store = KeyringGrantStore(fake)
        grant = VaultGrant.from_bytes(VAULT_ID, b"\x22" * 32, "bearer")
        store.save(grant)
        original = vault_grant.KeyringGrantStore.open_default
        vault_grant.KeyringGrantStore.open_default = classmethod(lambda cls: store)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                delete_local_grant_artifacts(tmp, VAULT_ID)
                self.assertIsNone(store.load(VAULT_ID))
        finally:
            vault_grant.KeyringGrantStore.open_default = original


if __name__ == "__main__":
    unittest.main()
