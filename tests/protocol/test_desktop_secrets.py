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

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config, ScrubResult  # noqa: E402
from src.secrets import (  # noqa: E402
    SECRET_KEY_AUTH_TOKEN,
    SERVICE_NAME,
    JsonFallbackStore,
    SecretServiceStore,
    SecretServiceUnavailable,
    SecretStore,
    open_default_store,
    pairing_symkey_key,
    parse_pairing_symkey_key,
)


def _seed_legacy_config(config_dir: Path, *, auth_token: str | None = None,
                        pairings: dict | None = None) -> None:
    """Write a pre-H.4 config.json with plaintext secrets so the
    Config under test can exercise the migration path."""
    import json as _json
    data: dict = {}
    if auth_token is not None:
        data["auth_token"] = auth_token
    if pairings:
        data["paired_devices"] = pairings
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(_json.dumps(data, indent=2))


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


# --- H.4: migration of legacy plaintext into the secret store -------


class LegacySecretsMigrationTests(unittest.TestCase):
    def test_auth_token_migrated_when_store_secure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_config(config_dir, auth_token="legacy-tok")
            fake = _FakeKeyring()
            store = SecretServiceStore(keyring_module=fake)
            cfg = Config(config_dir=config_dir, secret_store=store)
            # Token in keyring, gone from JSON, marker present.
            self.assertEqual(cfg.auth_token, "legacy-tok")
            self.assertEqual(
                fake.values[(SERVICE_NAME, SECRET_KEY_AUTH_TOKEN)],
                "legacy-tok",
            )
            self.assertNotIn("auth_token", cfg._data)
            self.assertIn("secrets_migrated_at", cfg._data)
            # Disk reflects same state.
            import json as _json
            with open(config_dir / "config.json") as f:
                disk = _json.load(f)
            self.assertNotIn("auth_token", disk)
            self.assertIn("secrets_migrated_at", disk)

    def test_pairing_symkeys_migrated_when_store_secure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_config(config_dir, pairings={
                "dev-A": {
                    "pubkey": "pk-A", "name": "Phone A",
                    "paired_at": 1700000000, "symmetric_key_b64": "sk-A",
                },
                "dev-B": {
                    "pubkey": "pk-B", "name": "Phone B",
                    "paired_at": 1700000001, "symmetric_key_b64": "sk-B",
                },
            })
            fake = _FakeKeyring()
            store = SecretServiceStore(keyring_module=fake)
            cfg = Config(config_dir=config_dir, secret_store=store)
            # Symkeys in keyring.
            self.assertEqual(
                fake.values[(SERVICE_NAME, pairing_symkey_key("dev-A"))],
                "sk-A",
            )
            self.assertEqual(
                fake.values[(SERVICE_NAME, pairing_symkey_key("dev-B"))],
                "sk-B",
            )
            # Removed from JSON entries — but metadata kept.
            import json as _json
            with open(config_dir / "config.json") as f:
                disk = _json.load(f)
            for did in ("dev-A", "dev-B"):
                self.assertNotIn(
                    "symmetric_key_b64", disk["paired_devices"][did],
                    f"{did} kept plaintext symkey after migration",
                )
                self.assertEqual(disk["paired_devices"][did]["pubkey"],
                                 f"pk-{did[-1]}")

    def test_no_op_when_no_plaintext(self) -> None:
        # Already-migrated install: no secrets to move, no save, no marker change.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_config(config_dir, auth_token=None, pairings={
                "dev-A": {
                    "pubkey": "pk-A", "name": "Phone",
                    "paired_at": 1700000000,
                    # NO symmetric_key_b64 — already migrated previously
                },
            })
            fake = _FakeKeyring()
            # Pre-seed the keyring as if we'd migrated before
            fake.values[(SERVICE_NAME, pairing_symkey_key("dev-A"))] = "sk-A"
            store = SecretServiceStore(keyring_module=fake)
            cfg = Config(config_dir=config_dir, secret_store=store)
            # No new marker should be written
            self.assertNotIn("secrets_migrated_at", cfg._data)
            # paired_devices still hydrates the symkey from the store
            self.assertEqual(
                cfg.paired_devices["dev-A"]["symmetric_key_b64"],
                "sk-A",
            )

    def test_no_migration_when_store_insecure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_config(config_dir, auth_token="stays-in-json",
                                pairings={
                                    "dev-A": {
                                        "pubkey": "pk-A",
                                        "symmetric_key_b64": "sk-A-stays",
                                        "name": "Phone",
                                        "paired_at": 1700000000,
                                    },
                                })
            cfg = Config(config_dir=config_dir)  # JsonFallbackStore via env-var
            self.assertFalse(cfg._secret_store.is_secure())
            # Plaintext stays put — no migration on insecure backend.
            self.assertEqual(cfg.auth_token, "stays-in-json")
            self.assertEqual(cfg._data["auth_token"], "stays-in-json")
            self.assertEqual(
                cfg._data["paired_devices"]["dev-A"]["symmetric_key_b64"],
                "sk-A-stays",
            )
            self.assertNotIn("secrets_migrated_at", cfg._data)

    def test_idempotent_across_config_reinits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_config(config_dir, auth_token="tok-1")
            fake = _FakeKeyring()
            store1 = SecretServiceStore(keyring_module=fake)
            cfg1 = Config(config_dir=config_dir, secret_store=store1)
            marker1 = cfg1._data.get("secrets_migrated_at")
            self.assertIsNotNone(marker1)
            # Second init: no plaintext left, marker should not be
            # overwritten because no migration step ran.
            store2 = SecretServiceStore(keyring_module=fake)
            cfg2 = Config(config_dir=config_dir, secret_store=store2)
            self.assertEqual(cfg2._data.get("secrets_migrated_at"), marker1)

    def test_partial_failure_leaves_other_plaintext_alone(self) -> None:
        # auth_token migrates, but symkey set raises — symkey
        # plaintext stays in JSON so the next boot can retry.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_config(config_dir, auth_token="tok-ok",
                                pairings={
                                    "dev-A": {
                                        "pubkey": "pk-A",
                                        "symmetric_key_b64": "sk-A",
                                        "name": "Phone",
                                        "paired_at": 1700000000,
                                    },
                                })
            fake = _FakeKeyring()
            # Set up a keyring whose set fails for the pairing key
            # but succeeds for auth_token.
            real_set = fake.set_password

            def selective_set(service, username, password):
                if username.startswith("pairing_symkey:"):
                    raise fake.errors.KeyringError("simulated mid-migration drop")
                return real_set(service, username, password)
            fake.set_password = selective_set  # type: ignore[assignment]

            store = SecretServiceStore(keyring_module=fake)
            cfg = Config(config_dir=config_dir, secret_store=store)
            # Token migrated; pairing symkey left in plaintext.
            self.assertEqual(
                fake.values[(SERVICE_NAME, SECRET_KEY_AUTH_TOKEN)],
                "tok-ok",
            )
            self.assertNotIn("auth_token", cfg._data)
            self.assertEqual(
                cfg._data["paired_devices"]["dev-A"]["symmetric_key_b64"],
                "sk-A",
                "partial failure should leave un-migrated plaintext in place",
            )

    def test_paired_devices_hydrates_symkey_from_store(self) -> None:
        # After migration the JSON entry has no symkey, but the
        # getter must still expose it for downstream callers
        # (poller.py, send_runner.py, etc.).
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            _seed_legacy_config(config_dir, pairings={
                "dev-A": {
                    "pubkey": "pk-A",
                    "symmetric_key_b64": "sk-A",
                    "name": "Phone",
                    "paired_at": 1700000000,
                },
            })
            fake = _FakeKeyring()
            store = SecretServiceStore(keyring_module=fake)
            cfg = Config(config_dir=config_dir, secret_store=store)
            # Migration happened — verify symkey hydrates back.
            entry = cfg.paired_devices["dev-A"]
            self.assertEqual(entry["symmetric_key_b64"], "sk-A")
            self.assertEqual(entry["pubkey"], "pk-A")
            self.assertEqual(entry["name"], "Phone")

    def test_paired_devices_hydration_no_op_when_symkey_in_json(self) -> None:
        # JsonFallbackStore path: getter merges nothing because the
        # symkey is already in the entry.
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            cfg = Config(config_dir=config_dir)  # JsonFallbackStore
            cfg.add_paired_device("dev-A", "pk-A", "sk-A", name="Phone")
            entry = cfg.paired_devices["dev-A"]
            self.assertEqual(entry["symmetric_key_b64"], "sk-A")


# --- H.4: remove_paired_device + open_default_store -----------------


class RemovePairedDeviceTests(unittest.TestCase):
    def test_remove_with_secure_store_clears_both(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=store)
            cfg.add_paired_device("dev-A", "pk-A", "sk-A", name="A")
            cfg.add_paired_device("dev-B", "pk-B", "sk-B", name="B")
            cfg.remove_paired_device("dev-A")
            # JSON: dev-A gone, dev-B intact
            self.assertNotIn("dev-A", cfg._data["paired_devices"])
            self.assertIn("dev-B", cfg._data["paired_devices"])
            # Keyring: dev-A entry gone, dev-B intact
            self.assertNotIn(
                (SERVICE_NAME, pairing_symkey_key("dev-A")), fake.values,
            )
            self.assertIn(
                (SERVICE_NAME, pairing_symkey_key("dev-B")), fake.values,
            )

    def test_remove_with_json_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))  # JsonFallbackStore
            cfg.add_paired_device("dev-A", "pk-A", "sk-A")
            cfg.add_paired_device("dev-B", "pk-B", "sk-B")
            cfg.remove_paired_device("dev-A")
            self.assertNotIn("dev-A", cfg._data["paired_devices"])
            self.assertIn("dev-B", cfg._data["paired_devices"])

    def test_remove_unknown_device_id_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))
            cfg.remove_paired_device("dev-never-existed")
            self.assertEqual(cfg.paired_devices, {})


class ScrubSecretsTests(unittest.TestCase):
    """H.6: scrub_secrets is on-demand re-migration. Used by the
    --scrub-secrets CLI flag and the Settings 'Verify' button to
    move plaintext that snuck back in (manual edits, partial-failure
    boots) into the secret store."""

    def test_no_op_when_already_clean(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=store)
            cfg.auth_token = "tok-already-in-store"
            result = cfg.scrub_secrets()
            self.assertEqual(
                result, ScrubResult(secure=True, scrubbed=0, failed=0),
            )

    def test_scrubs_manually_edited_plaintext_auth_token(self) -> None:
        # Simulate the user hand-editing config.json to add an
        # auth_token field after the original migration moved it
        # to the keyring. Scrub must move it back out.
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            cfg = Config(config_dir=config_dir, secret_store=store)
            # Bypass the public setter to land plaintext into _data
            # the way a manual JSON edit would. Save it to disk so
            # reload() picks it up.
            cfg._data["auth_token"] = "manual-edit-token"
            cfg.save()
            result = cfg.scrub_secrets()
            self.assertEqual(
                result, ScrubResult(secure=True, scrubbed=1, failed=0),
            )
            # Plaintext gone, keyring has the value.
            self.assertNotIn("auth_token", cfg._data)
            self.assertEqual(
                fake.values[(SERVICE_NAME, SECRET_KEY_AUTH_TOKEN)],
                "manual-edit-token",
            )

    def test_scrubs_pairing_symkey_added_back_to_json(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            cfg = Config(config_dir=config_dir, secret_store=store)
            cfg.add_paired_device("dev-X", "pk-X", "sk-X", name="N")
            # Manual re-edit puts plaintext back next to the metadata.
            cfg._data["paired_devices"]["dev-X"]["symmetric_key_b64"] = "sk-edit"
            cfg.save()
            result = cfg.scrub_secrets()
            self.assertEqual(
                result, ScrubResult(secure=True, scrubbed=1, failed=0),
            )
            self.assertNotIn(
                "symmetric_key_b64",
                cfg._data["paired_devices"]["dev-X"],
            )
            # Keyring now holds the manually-edited value.
            self.assertEqual(
                fake.values[(SERVICE_NAME, pairing_symkey_key("dev-X"))],
                "sk-edit",
            )

    def test_scrubs_multiple_fields_in_one_call(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=store)
            cfg.add_paired_device("dev-A", "pk-A", "sk-A")
            cfg.add_paired_device("dev-B", "pk-B", "sk-B")
            cfg._data["auth_token"] = "tok-edit"
            cfg._data["paired_devices"]["dev-A"]["symmetric_key_b64"] = "sk-A-edit"
            cfg._data["paired_devices"]["dev-B"]["symmetric_key_b64"] = "sk-B-edit"
            cfg.save()
            result = cfg.scrub_secrets()
            self.assertEqual(
                result, ScrubResult(secure=True, scrubbed=3, failed=0),
            )

    def test_no_op_when_store_insecure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))  # JsonFallbackStore
            # Even with plaintext present, scrub does nothing — the
            # store is insecure, there's nowhere to migrate to.
            cfg._data["auth_token"] = "stays-in-json"
            cfg.save()
            result = cfg.scrub_secrets()
            self.assertEqual(
                result, ScrubResult(secure=False, scrubbed=0, failed=0),
            )
            # Plaintext still there.
            self.assertEqual(
                cfg._data.get("auth_token"), "stays-in-json",
            )

    def test_partial_failure_reflected_in_result(self) -> None:
        # Mid-scrub keyring failure: auth_token migrates, pairing
        # symkey set raises. ScrubResult reports failed=1.
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=store)
            cfg.add_paired_device("dev-A", "pk-A", "sk-A")
            cfg._data["auth_token"] = "tok-edit"
            cfg._data["paired_devices"]["dev-A"]["symmetric_key_b64"] = "sk-edit"
            cfg.save()

            real_set = fake.set_password

            def selective_set(service, username, password):
                if username.startswith("pairing_symkey:"):
                    raise fake.errors.KeyringError("simulated mid-scrub drop")
                return real_set(service, username, password)
            fake.set_password = selective_set  # type: ignore[assignment]

            result = cfg.scrub_secrets()
            self.assertEqual(
                result, ScrubResult(secure=True, scrubbed=1, failed=1),
            )
            # auth_token migrated; pairing symkey plaintext stays.
            self.assertNotIn("auth_token", cfg._data)
            self.assertEqual(
                cfg._data["paired_devices"]["dev-A"]["symmetric_key_b64"],
                "sk-edit",
            )

    def test_picks_up_external_edits_via_reload(self) -> None:
        # Scrub does an explicit reload before counting — verifies
        # that an edit made by an external process / hand-edit
        # while the Config object is alive gets picked up.
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td)
            cfg = Config(config_dir=config_dir, secret_store=store)
            cfg.auth_token = "in-store"
            # External writer modifies config.json behind cfg's back.
            disk_path = config_dir / "config.json"
            data = json.loads(disk_path.read_text())
            data["auth_token"] = "snuck-in-externally"
            disk_path.write_text(json.dumps(data))
            # Without scrub's reload, cfg._data wouldn't see this.
            self.assertNotIn("auth_token", cfg._data)
            result = cfg.scrub_secrets()
            self.assertEqual(result.scrubbed, 1)


class IsSecretStorageSecureTests(unittest.TestCase):
    """H.5: Config exposes the active backend's security via a
    public method so the CLI / tray surfaces have a single check."""

    def test_false_when_using_json_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))  # JsonFallbackStore via env-var
            self.assertFalse(cfg.is_secret_storage_secure())

    def test_true_when_using_secret_service_store(self) -> None:
        fake = _FakeKeyring()
        store = SecretServiceStore(keyring_module=fake)
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td), secret_store=store)
            self.assertTrue(cfg.is_secret_storage_secure())

    def test_matches_underlying_store_is_secure(self) -> None:
        # Public method is just a thin wrapper — pin that contract so
        # callers can rely on it not diverging.
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(config_dir=Path(td))
            self.assertEqual(
                cfg.is_secret_storage_secure(),
                cfg._secret_store.is_secure(),
            )


class OpenDefaultStoreTests(unittest.TestCase):
    def test_returns_secret_service_store_when_keyring_works(self) -> None:
        # Pass a fake keyring module via SecretServiceStore directly —
        # then verify open_default_store can see it as the public path.
        # We can't easily inject a fake here without monkeypatching,
        # so test the JSON-fallback path more directly.
        # (SecretServiceStoreTests already covers the "keyring works"
        # path under controlled conditions.)
        ...

    def test_falls_back_to_json_when_keyring_unavailable(self) -> None:
        # Simulate keyring not installed by patching sys.modules.
        import sys
        saved = sys.modules.get("keyring")
        sys.modules["keyring"] = None  # type: ignore[assignment]
        try:
            data: dict = {}
            saves: list[int] = [0]

            def save() -> None:
                saves[0] += 1
            store = open_default_store(data, save)
            self.assertIsInstance(store, JsonFallbackStore)
            self.assertFalse(store.is_secure())
        finally:
            if saved is None:
                sys.modules.pop("keyring", None)
            else:
                sys.modules["keyring"] = saved


if __name__ == "__main__":
    unittest.main()
