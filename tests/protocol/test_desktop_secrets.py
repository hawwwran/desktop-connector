"""Tests for hardening-plan H.2 — secret-storage abstraction.

The abstraction lands in ``desktop/src/secrets.py`` with the
``JsonFallbackStore`` as its only implementation. On-disk shape is
byte-equivalent to pre-H.2: ``auth_token`` at the top of
``config.json``; per-pairing symmetric keys nested inside the
existing ``paired_devices`` dict next to ``pubkey``/``name``/
``paired_at``. These tests pin the abstraction's contract so H.3+
backends (libsecret) can swap in without changing call sites.
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

from src.config import Config  # noqa: E402
from src.secrets import (  # noqa: E402
    SECRET_KEY_AUTH_TOKEN,
    SERVICE_NAME,
    JsonFallbackStore,
    SecretServiceStore,
    SecretServiceUnavailable,
    SecretStore,
    pairing_symkey_key,
    parse_pairing_symkey_key,
)


class _RecordingStore:
    """In-memory ``SecretStore`` for testing the Config integration
    seam without touching the JSON file."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, str, str | None]] = []  # (op, key, value)

    def get(self, key: str) -> str | None:
        self.calls.append(("get", key, None))
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.calls.append(("set", key, value))
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.calls.append(("delete", key, None))
        self.values.pop(key, None)

    def is_secure(self) -> bool:
        return True


class JsonFallbackStoreTests(unittest.TestCase):
    def _store(self) -> tuple[JsonFallbackStore, dict, list[int]]:
        data: dict = {}
        save_count = [0]

        def save() -> None:
            save_count[0] += 1

        return JsonFallbackStore(data, save), data, save_count

    def test_is_secure_false(self) -> None:
        store, _data, _ = self._store()
        self.assertFalse(store.is_secure())

    def test_auth_token_round_trip(self) -> None:
        store, data, saves = self._store()
        self.assertIsNone(store.get(SECRET_KEY_AUTH_TOKEN))
        store.set(SECRET_KEY_AUTH_TOKEN, "tok-001")
        self.assertEqual(store.get(SECRET_KEY_AUTH_TOKEN), "tok-001")
        self.assertEqual(data["auth_token"], "tok-001")
        self.assertEqual(saves[0], 1)
        store.delete(SECRET_KEY_AUTH_TOKEN)
        self.assertIsNone(store.get(SECRET_KEY_AUTH_TOKEN))
        self.assertNotIn("auth_token", data)
        self.assertEqual(saves[0], 2)

    def test_pairing_symkey_round_trip(self) -> None:
        store, data, _ = self._store()
        key = pairing_symkey_key("dev-A")
        store.set(key, "sk-A")
        self.assertEqual(store.get(key), "sk-A")
        self.assertEqual(
            data["paired_devices"]["dev-A"]["symmetric_key_b64"],
            "sk-A",
        )

    def test_pairing_symkey_delete_preserves_metadata(self) -> None:
        store, data, _ = self._store()
        # Pretend a paired device exists with metadata + symkey
        data["paired_devices"] = {
            "dev-A": {
                "pubkey": "pk-A",
                "name": "Phone",
                "paired_at": 1700000000,
                "symmetric_key_b64": "sk-A",
            }
        }
        store.delete(pairing_symkey_key("dev-A"))
        # symkey gone, metadata intact
        entry = data["paired_devices"]["dev-A"]
        self.assertNotIn("symmetric_key_b64", entry)
        self.assertEqual(entry["pubkey"], "pk-A")
        self.assertEqual(entry["name"], "Phone")
        self.assertEqual(entry["paired_at"], 1700000000)

    def test_get_unknown_key_returns_none(self) -> None:
        store, _data, _ = self._store()
        self.assertIsNone(store.get("not_a_real_secret"))

    def test_set_unknown_key_raises(self) -> None:
        store, _data, _ = self._store()
        with self.assertRaises(ValueError):
            store.set("nonsense", "x")

    def test_delete_unknown_key_raises(self) -> None:
        store, _data, _ = self._store()
        with self.assertRaises(ValueError):
            store.delete("nonsense")

    def test_delete_missing_does_not_save(self) -> None:
        # Calling delete for a key that isn't present should not
        # trigger a needless save — keeps no-op writes off disk.
        store, _data, saves = self._store()
        store.delete(SECRET_KEY_AUTH_TOKEN)
        self.assertEqual(saves[0], 0)
        store.delete(pairing_symkey_key("nonexistent"))
        self.assertEqual(saves[0], 0)

    def test_parse_round_trip(self) -> None:
        self.assertEqual(parse_pairing_symkey_key(pairing_symkey_key("X")), "X")
        self.assertIsNone(parse_pairing_symkey_key(SECRET_KEY_AUTH_TOKEN))
        self.assertIsNone(parse_pairing_symkey_key("random-string"))


class ConfigIntegrationTests(unittest.TestCase):
    def test_default_store_is_json_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))
            self.assertIsInstance(cfg._secret_store, JsonFallbackStore)
            self.assertFalse(cfg._secret_store.is_secure())

    def test_auth_token_persists_byte_equivalent(self) -> None:
        # Pre-H.2: auth_token sits at the top of config.json.
        # Verify the new code path lands it in the same place.
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))
            cfg.auth_token = "tok-001"
            cfg2 = Config(config_dir=Path(td))
            self.assertEqual(cfg2.auth_token, "tok-001")
            import json
            with open(Path(td) / "config.json") as f:
                disk = json.load(f)
            self.assertEqual(disk["auth_token"], "tok-001")

    def test_add_paired_device_preserves_disk_shape(self) -> None:
        # Pre-H.2: paired_devices[<id>] = {pubkey, symmetric_key_b64,
        # name, paired_at}. The H.2 refactor must keep that shape.
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))
            cfg.add_paired_device("dev-A", "pk-A", "sk-A", name="Phone")
            cfg2 = Config(config_dir=Path(td))
            entry = cfg2.paired_devices["dev-A"]
            self.assertEqual(entry["pubkey"], "pk-A")
            self.assertEqual(entry["symmetric_key_b64"], "sk-A")
            self.assertEqual(entry["name"], "Phone")
            self.assertIn("paired_at", entry)

    def test_wipe_credentials_pairing_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))
            cfg.auth_token = "tok-001"
            cfg.device_id = "dev-self"
            cfg.add_paired_device("dev-A", "pk-A", "sk-A")
            cfg.wipe_credentials("pairing_only")
            self.assertEqual(cfg.paired_devices, {})
            self.assertEqual(cfg.auth_token, "tok-001")  # preserved
            self.assertEqual(cfg.device_id, "dev-self")  # preserved

    def test_wipe_credentials_full(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))
            cfg.auth_token = "tok-001"
            cfg.device_id = "dev-self"
            cfg.add_paired_device("dev-A", "pk-A", "sk-A")
            cfg.wipe_credentials("full")
            self.assertEqual(cfg.paired_devices, {})
            self.assertIsNone(cfg.auth_token)
            self.assertIsNone(cfg.device_id)

    def test_inject_custom_store_routes_auth_token(self) -> None:
        # Verify the seam: Config asks the injected store for the
        # secret rather than reading _data directly.
        fake = _RecordingStore()
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=fake)
            cfg.auth_token = "tok-via-fake"
            self.assertEqual(fake.values.get(SECRET_KEY_AUTH_TOKEN), "tok-via-fake")
            self.assertEqual(cfg.auth_token, "tok-via-fake")
            # And the JSON dict should NOT carry a duplicate copy when
            # a non-JSON-fallback store is injected — exercises the
            # H.3+ contract preview.
            self.assertNotIn("auth_token", cfg._data)

    def test_inject_custom_store_routes_pairing_symkey(self) -> None:
        fake = _RecordingStore()
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=fake)
            cfg.add_paired_device("dev-A", "pk-A", "sk-A", name="Phone")
            self.assertEqual(
                fake.values.get(pairing_symkey_key("dev-A")),
                "sk-A",
            )
            # Non-secret metadata still in _data
            entry = cfg._data["paired_devices"]["dev-A"]
            self.assertEqual(entry["pubkey"], "pk-A")
            self.assertEqual(entry["name"], "Phone")
            self.assertNotIn("symmetric_key_b64", entry)

    def test_reload_preserves_secret_store(self) -> None:
        # Reload mutates _data in place rather than reassigning the
        # attribute, so the JsonFallbackStore's reference stays
        # valid. Pin that.
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))
            cfg.auth_token = "tok-pre"
            store_id_before = id(cfg._secret_store)
            data_id_before = id(cfg._data)
            cfg.reload()
            self.assertEqual(id(cfg._secret_store), store_id_before)
            self.assertEqual(id(cfg._data), data_id_before)
            self.assertEqual(cfg.auth_token, "tok-pre")


# Make the SecretStore Protocol explicit by checking JsonFallbackStore
# satisfies it at the type level. Instances are duck-typed at runtime;
# this assertion just keeps mypy / readers honest.
def _accepts_store(store: SecretStore) -> str | None:
    return store.get(SECRET_KEY_AUTH_TOKEN)


class ProtocolConformanceTests(unittest.TestCase):
    def test_json_fallback_satisfies_secret_store(self) -> None:
        store = JsonFallbackStore({}, lambda: None)
        # Should be invokable as a SecretStore — Protocol conformance
        # is structural in Python, but the test pins it explicitly so
        # changes to the Protocol surface are noisy.
        self.assertIsNone(_accepts_store(store))


# --- H.3: SecretServiceStore (fake keyring module) ------------------


class _FakeKeyringErrors:
    """Mirror of ``keyring.errors`` namespace used by the fake."""

    class PasswordDeleteError(Exception):
        pass

    class KeyringError(Exception):
        pass

    class NoKeyringError(KeyringError):
        pass


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` package.

    Only the four functions :class:`SecretServiceStore` calls are
    implemented; ``errors`` exposes ``PasswordDeleteError`` so the
    delete-no-such-entry path can be exercised exactly as it is in
    real ``keyring``.
    """

    errors = _FakeKeyringErrors

    def __init__(self) -> None:
        # Map (service, username) -> password.
        self.values: dict[tuple[str, str], str] = {}
        # Operation log for assertions.
        self.calls: list[tuple[str, str, str]] = []
        # Switches for failure-injection tests.
        self.fail_get_with: Exception | None = None
        self.fail_set_with: Exception | None = None
        self.fail_delete_with: Exception | None = None

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", service, username))
        if self.fail_get_with is not None:
            raise self.fail_get_with
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set", service, username))
        if self.fail_set_with is not None:
            raise self.fail_set_with
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username))
        if self.fail_delete_with is not None:
            raise self.fail_delete_with
        if (service, username) not in self.values:
            raise self.errors.PasswordDeleteError("no such entry")
        del self.values[(service, username)]


class SecretServiceStoreTests(unittest.TestCase):
    def test_probe_succeeds_on_reachable_backend(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        self.assertTrue(store.is_secure())
        # Probe used the canonical service name.
        self.assertIn(("get", SERVICE_NAME, "_probe"), fake.calls)

    def test_probe_fails_with_typed_exception(self) -> None:
        fake = _FakeKeyring()
        fake.fail_get_with = fake.errors.NoKeyringError("no D-Bus session")
        with self.assertRaises(SecretServiceUnavailable) as cm:
            SecretServiceStore(keyring_module=fake)
        self.assertIn("no D-Bus session", str(cm.exception))

    def test_auth_token_round_trip(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        self.assertIsNone(store.get(SECRET_KEY_AUTH_TOKEN))
        store.set(SECRET_KEY_AUTH_TOKEN, "tok-svc")
        self.assertEqual(store.get(SECRET_KEY_AUTH_TOKEN), "tok-svc")
        # Stored under the canonical service+key tuple — verifiable
        # in seahorse / kwalletmanager.
        self.assertEqual(
            fake.values[(SERVICE_NAME, SECRET_KEY_AUTH_TOKEN)],
            "tok-svc",
        )
        store.delete(SECRET_KEY_AUTH_TOKEN)
        self.assertIsNone(store.get(SECRET_KEY_AUTH_TOKEN))

    def test_pairing_symkey_round_trip(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        key = pairing_symkey_key("dev-A")
        store.set(key, "sk-svc")
        self.assertEqual(store.get(key), "sk-svc")
        # Visible in the keyring under the same flat namespace as the
        # JSON fallback uses (modulo the prefix).
        self.assertEqual(fake.values[(SERVICE_NAME, key)], "sk-svc")

    def test_delete_missing_is_no_op(self) -> None:
        # Matches JsonFallbackStore's "delete missing = no-op" semantics.
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        # Should not raise even though no entry exists.
        store.delete(SECRET_KEY_AUTH_TOKEN)
        store.delete(pairing_symkey_key("never-existed"))

    def test_get_runtime_failure_surfaces_typed_exception(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        fake.fail_get_with = fake.errors.KeyringError("mid-session lock")
        with self.assertRaises(SecretServiceUnavailable):
            store.get(SECRET_KEY_AUTH_TOKEN)

    def test_set_runtime_failure_surfaces_typed_exception(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        fake.fail_set_with = fake.errors.KeyringError("backend dropped")
        with self.assertRaises(SecretServiceUnavailable):
            store.set(SECRET_KEY_AUTH_TOKEN, "tok")

    def test_delete_unexpected_failure_surfaces_typed_exception(self) -> None:
        # Failure other than PasswordDeleteError should surface as
        # SecretServiceUnavailable, not as the raw backend error.
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        # Pre-populate so delete reaches the actual delete call path
        # rather than the missing-entry no-op.
        store.set(SECRET_KEY_AUTH_TOKEN, "tok")
        fake.fail_delete_with = fake.errors.KeyringError("permission denied")
        with self.assertRaises(SecretServiceUnavailable):
            store.delete(SECRET_KEY_AUTH_TOKEN)

    def test_missing_keyring_package_raises_typed_exception(self) -> None:
        # Simulate dev tree without the `keyring` package — passing a
        # sentinel that isn't a module triggers the lazy-import path,
        # so we patch sys.modules to make `import keyring` fail.
        import sys
        sentinel = object()
        saved = sys.modules.get("keyring")
        # Force ImportError at the lazy-import inside __init__.
        sys.modules["keyring"] = None  # type: ignore[assignment]
        try:
            with self.assertRaises(SecretServiceUnavailable) as cm:
                SecretServiceStore()
            self.assertIn("keyring", str(cm.exception).lower())
        finally:
            if saved is None:
                sys.modules.pop("keyring", None)
            else:
                sys.modules["keyring"] = saved
        # sentinel kept alive to stop pyflakes from removing it
        del sentinel

    def test_satisfies_secret_store_protocol(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        self.assertIsNone(_accepts_store(store))


# --- H.3: Config integration with SecretServiceStore ----------------


class ConfigWithSecretServiceTests(unittest.TestCase):
    """Same call sites that exercise JsonFallbackStore in earlier
    tests must work transparently with SecretServiceStore. The
    seam from H.2 is the contract."""

    def test_auth_token_through_secret_service(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=store)
            cfg.auth_token = "tok-svc"
            self.assertEqual(cfg.auth_token, "tok-svc")
            # No leak to the JSON dict — H.3's whole point.
            self.assertNotIn("auth_token", cfg._data)

    def test_add_paired_device_through_secret_service(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=store)
            cfg.add_paired_device("dev-A", "pk-A", "sk-svc", name="Phone")
            # Symkey landed in the keyring, NOT in JSON.
            self.assertEqual(
                fake.values[(SERVICE_NAME, pairing_symkey_key("dev-A"))],
                "sk-svc",
            )
            self.assertNotIn(
                "symmetric_key_b64",
                cfg._data["paired_devices"]["dev-A"],
            )
            # Non-secret metadata stays in JSON.
            entry = cfg._data["paired_devices"]["dev-A"]
            self.assertEqual(entry["pubkey"], "pk-A")
            self.assertEqual(entry["name"], "Phone")
            self.assertIn("paired_at", entry)

    def test_wipe_credentials_clears_keyring_entries(self) -> None:
        # Critical: without the H.2 wipe_credentials refactor through
        # the store, a libsecret backend would leak orphan entries.
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=store)
            cfg.auth_token = "tok"
            cfg.add_paired_device("dev-A", "pk-A", "sk-A")
            cfg.add_paired_device("dev-B", "pk-B", "sk-B")
            cfg.wipe_credentials("full")
            # No keyring entries should remain under the service name.
            for (service, _key) in fake.values:
                self.assertNotEqual(
                    service, SERVICE_NAME,
                    "orphan keyring entry survived wipe_credentials('full')",
                )


if __name__ == "__main__":
    unittest.main()
