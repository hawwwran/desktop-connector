"""T17.5 — Debug bundle: redact, package, refuse if a leak survives."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_debug_bundle import (  # noqa: E402
    DebugBundleError, FORBIDDEN_PATTERNS, REDACTED,
    build_debug_bundle_bytes, redact_config, scan_for_forbidden,
    schema_dump, tail_lines, write_debug_bundle,
)


class RedactConfigTests(unittest.TestCase):
    def test_known_keys_are_redacted_keys_renamed(self) -> None:
        config = {
            "auth_token": "abcd1234",
            "device_id": "publicly-fine",
            "vault_access_secret": "super-secret-bytes",
            "recovery_passphrase": "purple monkey dishwasher",
            "find_phone_password": "rotor-fish",
            "keys": {"x25519": "secret-key-bytes"},
        }
        redacted = redact_config(config)
        # Sensitive keys are renamed (so a literal grep for the field
        # name returns nothing) AND their values redacted.
        self.assertNotIn("auth_token", redacted)
        self.assertNotIn("vault_access_secret", redacted)
        self.assertNotIn("recovery_passphrase", redacted)
        self.assertNotIn("keys", redacted)
        # Public keys survive verbatim.
        self.assertEqual(redacted["device_id"], "publicly-fine")
        # Multiple sensitive keys collapse onto the redacted-field
        # placeholder family. Every value should be REDACTED.
        for k, v in redacted.items():
            if k.startswith("<redacted-field>"):
                self.assertEqual(v, REDACTED)

    def test_recursion_into_nested_dicts(self) -> None:
        config = {
            "vault": {
                "active": True,
                "recovery_kit_path": "/secret/path",
                "vault_access_secret": "leaks-here",
            },
            "harmless": [1, 2, {"nested_secret": "x"}],
        }
        redacted = redact_config(config)
        self.assertEqual(redacted["vault"]["active"], True)
        # Sensitive keys removed from the nested dict.
        self.assertNotIn("recovery_kit_path", redacted["vault"])
        self.assertNotIn("vault_access_secret", redacted["vault"])
        # The list passes through; nested dicts inside still get
        # redacted.
        self.assertNotIn("nested_secret", redacted["harmless"][2])

    def test_lists_pass_through_recursively(self) -> None:
        config = {
            "paired_devices": [
                {"device_id": "d1", "secret_blob": "leak"},
            ],
        }
        redacted = redact_config(config)
        # paired_devices is in REDACT_KEYS so the whole list is redacted
        # (key renamed).
        self.assertNotIn("paired_devices", redacted)


class ScanForbiddenTests(unittest.TestCase):
    def test_scan_finds_known_substrings(self) -> None:
        text = "X-Vault-Authorization: Bearer abc123def\nelse"
        hits = scan_for_forbidden(text)
        self.assertNotEqual(hits, [])

    def test_scan_clean_payload_returns_empty(self) -> None:
        text = "vault.sync.binding_paused binding=rb_v1_a\n"
        self.assertEqual(scan_for_forbidden(text), [])

    def test_bytes_input_is_decoded(self) -> None:
        # The scan now targets value-shaped leaks. A header with an
        # unredacted value should fire.
        self.assertNotEqual(
            scan_for_forbidden(b"Authorization: Bearer abcdef0123456789"),
            [],
        )


class SchemaDumpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_debug_schema_"))
        self.dbpath = self.tmpdir / "test.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_schema_dump_excludes_row_data(self) -> None:
        conn = sqlite3.connect(self.dbpath)
        conn.execute(
            "CREATE TABLE secrets (id INTEGER PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT INTO secrets (value) VALUES ('plaintext-secret-data')"
        )
        conn.commit()
        conn.close()

        dump = schema_dump(self.dbpath)
        self.assertIn("CREATE TABLE secrets", dump)
        self.assertNotIn("plaintext-secret-data", dump)


class TailLinesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_debug_tail_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_only_last_n_bytes(self) -> None:
        log = self.tmpdir / "log.txt"
        log.write_text("first half\n" + "x" * 200_000 + "tail-marker\n")
        result = tail_lines(log, max_bytes=1000)
        self.assertIn("tail-marker", result)
        self.assertNotIn("first half", result)


class BuildBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_debug_bundle_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_zip_member(self, payload: bytes, name: str) -> str:
        with zipfile.ZipFile(BytesIO(payload)) as zf:
            with zf.open(name) as fh:
                return fh.read().decode("utf-8")

    def test_bundle_writes_redacted_config_and_skips_secrets(self) -> None:
        config = {
            "auth_token": "leaks-if-not-redacted",
            "device_id": "public",
        }
        payload = build_debug_bundle_bytes(config=config)
        body = self._read_zip_member(payload, "config.redacted.json")
        loaded = json.loads(body)
        # Sensitive key renamed; original substring absent.
        self.assertNotIn("auth_token", loaded)
        self.assertNotIn("leaks-if-not-redacted", body)
        # Public key intact.
        self.assertEqual(loaded["device_id"], "public")

    def test_bundle_packages_schema_dump(self) -> None:
        dbpath = self.tmpdir / "vault-local-index.sqlite3"
        conn = sqlite3.connect(dbpath)
        conn.execute("CREATE TABLE vault_bindings (binding_id TEXT)")
        conn.commit()
        conn.close()
        payload = build_debug_bundle_bytes(db_path=dbpath)
        body = self._read_zip_member(payload, "index_schema.txt")
        self.assertIn("CREATE TABLE vault_bindings", body)

    def test_bundle_includes_binding_states(self) -> None:
        states = [
            {"binding_id": "rb_v1_a", "state": "bound", "sync_mode": "two-way"},
        ]
        payload = build_debug_bundle_bytes(binding_states=states)
        body = self._read_zip_member(payload, "binding_states.json")
        self.assertIn("rb_v1_a", body)

    def test_bundle_attaches_activity_tail(self) -> None:
        log = self.tmpdir / "vault.log"
        log.write_text("vault.sync.binding_paused binding=rb_v1_x\n")
        payload = build_debug_bundle_bytes(activity_log_path=log)
        body = self._read_zip_member(payload, "activity_tail.txt")
        self.assertIn("binding_paused", body)

    def test_bundle_redacts_forbidden_text_in_innocent_field(self) -> None:
        # F-505: redact_config now scrubs scalar string values that
        # match FORBIDDEN_PATTERNS, so a leak in an "innocent_field"
        # key is rewritten before the bundle is built. The leak-scan
        # is the second-line defence; if it does spot something the
        # scrubber missed, build_debug_bundle_bytes still raises
        # DebugBundleError.
        config = {
            "innocent_field": (
                "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9_leak"
            ),
        }
        payload = build_debug_bundle_bytes(config=config)
        body = self._read_zip_member(payload, "config.redacted.json")
        # The Bearer-shaped substring is now scrubbed by F-505's
        # scalar-value pass; the leak-scan never sees a forbidden
        # token in the final bundle.
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9_leak", body)
        self.assertIn("redacted", body.lower())

    def test_write_debug_bundle_atomic_to_destination(self) -> None:
        dest = self.tmpdir / "out.zip"
        write_debug_bundle(dest, config={"device_id": "x"})
        self.assertTrue(dest.is_file())
        # The .tmp shouldn't exist post-replace.
        self.assertFalse(dest.with_suffix(".zip.tmp").exists())

    def test_full_grep_check_against_acceptance_keywords(self) -> None:
        """T17.5 acceptance: the bundle must not contain any of these tokens."""
        payload = build_debug_bundle_bytes(
            config={
                "auth_token": "abc",
                "vault_access_secret": "shouldnt-leak",
                "keys": {"k1": "secret-bytes"},
            },
            binding_states=[{"binding_id": "rb_v1_x", "state": "bound"}],
        )
        for forbidden in (
            b"vault_master_key", b"recovery", b"passphrase",
            b"Authorization:",  b"shouldnt-leak",
        ):
            with zipfile.ZipFile(BytesIO(payload)) as zf:
                for info in zf.infolist():
                    with zf.open(info) as fh:
                        self.assertNotIn(
                            forbidden, fh.read(),
                            f"{info.filename} contains forbidden bytes: {forbidden!r}",
                        )


if __name__ == "__main__":
    unittest.main()
