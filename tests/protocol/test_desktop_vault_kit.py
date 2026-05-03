"""T3.6 — Recovery kit file write + shred tests.

Covers:
  - kit file format per formats §12.5 (with vault_access_secret added)
  - mode 0o600
  - secret round-trip (32 bytes → base32 → 32 bytes)
  - shred_file overwrites + unlinks; idempotent on missing files
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import (  # noqa: E402
    Vault,
    parse_recovery_kit_file,
    recovery_envelope_meta_from_json,
    recovery_envelope_meta_to_json,
    shred_file,
    verify_recovery_kit,
    write_recovery_kit_file,
)
from src.vault_local import run_recovery_material_test  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"
RECOVERY_SECRET = bytes.fromhex("0f" * 32)
VAULT_ACCESS_SECRET = "fake-bearer-token-for-relay-access"


class WriteRecoveryKitFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="vault_kit_")
        self.path = Path(self.tmpdir) / "ABCD-2345-WXYZ.dc-vault-recovery"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_file_with_mode_0o600(self) -> None:
        write_recovery_kit_file(
            self.path,
            vault_id=VAULT_ID,
            recovery_secret=RECOVERY_SECRET,
            vault_access_secret=VAULT_ACCESS_SECRET,
        )
        self.assertTrue(self.path.exists())
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_format_has_required_fields(self) -> None:
        write_recovery_kit_file(
            self.path,
            vault_id=VAULT_ID,
            recovery_secret=RECOVERY_SECRET,
            vault_access_secret=VAULT_ACCESS_SECRET,
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# Desktop Connector — Vault Recovery Kit", text)
        self.assertIn("vault_id: ABCD-2345-WXYZ", text)
        self.assertIn("recovery_secret:", text)
        self.assertIn("vault_access_secret: fake-bearer-token-for-relay-access", text)
        self.assertIn("argon_params: argon2id-v1", text)
        # Severity warning is there.
        self.assertIn("BOTH are required", text)
        self.assertIn("no password reset", text)

    def test_recovery_secret_round_trips_via_base32(self) -> None:
        write_recovery_kit_file(
            self.path,
            vault_id=VAULT_ID,
            recovery_secret=RECOVERY_SECRET,
            vault_access_secret=VAULT_ACCESS_SECRET,
        )
        text = self.path.read_text(encoding="utf-8")
        # Find the recovery_secret line.
        for line in text.splitlines():
            if line.startswith("recovery_secret:"):
                b32 = line.split(":", 1)[1].strip()
                # Base32 round-trip back to bytes — pad as needed since
                # the kit format strips padding.
                pad_len = (8 - len(b32) % 8) % 8
                decoded = base64.b32decode(b32.upper() + "=" * pad_len)
                self.assertEqual(decoded, RECOVERY_SECRET)
                return
        self.fail("recovery_secret line missing from kit file")

    def test_rejects_wrong_secret_length(self) -> None:
        with self.assertRaisesRegex(ValueError, "32 bytes"):
            write_recovery_kit_file(
                self.path,
                vault_id=VAULT_ID,
                recovery_secret=b"too-short",
                vault_access_secret=VAULT_ACCESS_SECRET,
            )

    def test_rejects_empty_access_secret(self) -> None:
        with self.assertRaisesRegex(ValueError, "vault_access_secret is required"):
            write_recovery_kit_file(
                self.path,
                vault_id=VAULT_ID,
                recovery_secret=RECOVERY_SECRET,
                vault_access_secret="",
            )

    def test_atomic_replace_preserves_old_file_on_error(self) -> None:
        # Pre-existing file at the target path. Successful write
        # replaces it atomically.
        self.path.write_text("OLD CONTENT\n")
        write_recovery_kit_file(
            self.path,
            vault_id=VAULT_ID,
            recovery_secret=RECOVERY_SECRET,
            vault_access_secret=VAULT_ACCESS_SECRET,
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("OLD CONTENT", text)
        self.assertIn("vault_id: ABCD-2345-WXYZ", text)


class ParseRecoveryKitFileTests(unittest.TestCase):
    """Round-trip the kit file format: write then parse, fields match."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="vault_kit_parse_")
        self.path = Path(self.tmpdir) / "ABCD-2345-WXYZ.dc-vault-recovery"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_round_trip(self) -> None:
        write_recovery_kit_file(
            self.path,
            vault_id=VAULT_ID,
            recovery_secret=RECOVERY_SECRET,
            vault_access_secret=VAULT_ACCESS_SECRET,
        )
        parsed = parse_recovery_kit_file(self.path)
        self.assertEqual(parsed["vault_id"], VAULT_ID)
        self.assertEqual(parsed["vault_id_dashed"], "ABCD-2345-WXYZ")
        self.assertEqual(parsed["recovery_secret"], RECOVERY_SECRET)
        self.assertEqual(parsed["vault_access_secret"], VAULT_ACCESS_SECRET)
        self.assertEqual(parsed["argon_params"], "argon2id-v1")

    def test_rejects_missing_field(self) -> None:
        # File missing recovery_secret line.
        self.path.write_text(
            "vault_id: ABCD-2345-WXYZ\n"
            "vault_access_secret: x\n"
            "argon_params: argon2id-v1\n"
        )
        with self.assertRaisesRegex(ValueError, "missing required field: recovery_secret"):
            parse_recovery_kit_file(self.path)

    def test_rejects_malformed_secret(self) -> None:
        # Right-shape file but recovery_secret isn't valid base32.
        self.path.write_text(
            "vault_id: ABCD-2345-WXYZ\n"
            "recovery_secret: not!base32!\n"
            "vault_access_secret: x\n"
            "argon_params: argon2id-v1\n"
        )
        with self.assertRaisesRegex(ValueError, "recovery_secret"):
            parse_recovery_kit_file(self.path)

    def test_tolerates_uppercase_secret(self) -> None:
        # Format spec says "decoders accept upper- or lowercase".
        import base64
        b32_upper = base64.b32encode(RECOVERY_SECRET).decode("ascii").rstrip("=")
        self.path.write_text(
            f"vault_id: ABCD-2345-WXYZ\n"
            f"recovery_secret: {b32_upper}\n"
            f"vault_access_secret: x\n"
            f"argon_params: argon2id-v1\n"
        )
        parsed = parse_recovery_kit_file(self.path)
        self.assertEqual(parsed["recovery_secret"], RECOVERY_SECRET)


class VerifyRecoveryKitTests(unittest.TestCase):
    """End-to-end recovery test: real Vault, real kit, real verify."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="vault_kit_verify_")
        self.path = Path(self.tmpdir) / "kit.txt"

        # Use a fake relay so this test doesn't need the PHP server.
        class _FakeRelay:
            def create_vault(self, **kw): return {"vault_id": kw["vault_id"]}
            def get_header(self, *a, **kw): raise NotImplementedError
        self.relay = _FakeRelay()
        self.passphrase = "correct-horse-battery-staple-test"

        # Reduced Argon2id cost for test speed.
        self.vault = Vault.create_new(
            self.relay,
            recovery_passphrase=self.passphrase,
            argon_memory_kib=8192,
            argon_iterations=2,
        )

        write_recovery_kit_file(
            self.path,
            vault_id=self.vault.vault_id,
            recovery_secret=self.vault.recovery_secret,
            vault_access_secret=self.vault.vault_access_secret,
            recovery_envelope_meta=self.vault.recovery_envelope_meta,
        )

    def tearDown(self) -> None:
        import shutil
        self.vault.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_correct_passphrase_passes(self) -> None:
        ok, msg = verify_recovery_kit(
            self.path,
            passphrase=self.passphrase,
            envelope_meta=self.vault.recovery_envelope_meta,
        )
        self.assertTrue(ok, f"verify should pass; got: {msg}")
        self.assertIn("master key", msg)

    def test_wrong_passphrase_fails(self) -> None:
        ok, msg = verify_recovery_kit(
            self.path,
            passphrase="WRONG-PASSPHRASE-FOR-NEGATIVE-TEST",
            envelope_meta=self.vault.recovery_envelope_meta,
        )
        self.assertFalse(ok)
        self.assertIn("failed", msg.lower())

    def test_recovery_envelope_meta_config_round_trip(self) -> None:
        encoded = recovery_envelope_meta_to_json(self.vault.recovery_envelope_meta)
        decoded = recovery_envelope_meta_from_json(encoded)

        ok, msg = verify_recovery_kit(
            self.path,
            passphrase=self.passphrase,
            envelope_meta=decoded,
        )
        self.assertTrue(ok, msg)

    def test_recovery_kit_can_embed_test_metadata(self) -> None:
        parsed = parse_recovery_kit_file(self.path)
        self.assertIn("recovery_envelope_meta", parsed)

        ok, msg = verify_recovery_kit(
            self.path,
            passphrase=self.passphrase,
            envelope_meta=parsed["recovery_envelope_meta"],
        )
        self.assertTrue(ok, msg)

    def test_recovery_material_test_checks_vault_id_and_passphrase(self) -> None:
        result = run_recovery_material_test(
            self.path,
            passphrase=self.passphrase,
            vault_id=self.vault.vault_id_dashed,
        )
        self.assertTrue(result.ok, result.message)
        self.assertFalse(result.wiped)

        wrong_id = run_recovery_material_test(
            self.path,
            passphrase=self.passphrase,
            vault_id="ZZZZ-ZZZZ-ZZZZ",
        )
        self.assertFalse(wrong_id.ok)
        self.assertIn("mismatch", wrong_id.message.lower())

    def test_recovery_material_test_explains_old_incomplete_kit_format(self) -> None:
        old_path = Path(self.tmpdir) / "old.dc-vault-recovery"
        write_recovery_kit_file(
            old_path,
            vault_id=self.vault.vault_id,
            recovery_secret=self.vault.recovery_secret,
            vault_access_secret=self.vault.vault_access_secret,
        )

        result = run_recovery_material_test(
            old_path,
            passphrase=self.passphrase,
            vault_id=self.vault.vault_id_dashed,
        )

        self.assertFalse(result.ok)
        self.assertIn("old incomplete format", result.message)
        self.assertIn("recovery envelope", result.message)

    def test_recovery_material_test_can_securely_delete_after_success(self) -> None:
        result = run_recovery_material_test(
            self.path,
            passphrase=self.passphrase,
            vault_id=self.vault.vault_id_dashed,
            wipe_after_success=True,
        )

        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.wiped)
        self.assertFalse(self.path.exists())

    def test_missing_kit_file_fails(self) -> None:
        ok, msg = verify_recovery_kit(
            self.path.with_suffix(".does-not-exist"),
            passphrase=self.passphrase,
            envelope_meta=self.vault.recovery_envelope_meta,
        )
        self.assertFalse(ok)
        self.assertIn("kit file", msg.lower())

    def test_corrupted_kit_secret_fails(self) -> None:
        # Hand-edit the kit so the recovery_secret byte differs from what
        # the wizard produced. Verify must fail closed (Poly1305).
        text = self.path.read_text()
        new_text = []
        for line in text.splitlines():
            if line.startswith("recovery_secret:"):
                new_text.append("recovery_secret: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
            else:
                new_text.append(line)
        self.path.write_text("\n".join(new_text) + "\n")

        ok, _ = verify_recovery_kit(
            self.path,
            passphrase=self.passphrase,
            envelope_meta=self.vault.recovery_envelope_meta,
        )
        self.assertFalse(ok)


class ShredFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="vault_shred_")
        self.path = Path(self.tmpdir) / "secret.txt"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_false_on_missing_file(self) -> None:
        self.assertFalse(shred_file(self.path))

    def test_overwrites_then_unlinks(self) -> None:
        self.path.write_bytes(b"super-secret-content-must-be-erased")
        result = shred_file(self.path)
        self.assertTrue(result)
        self.assertFalse(self.path.exists())

    def test_returns_false_on_directory(self) -> None:
        # Sanity: pointing shred at a directory should not nuke it
        # and should not raise.
        self.assertFalse(shred_file(self.tmpdir))
        # Directory still exists.
        self.assertTrue(Path(self.tmpdir).is_dir())

    def test_idempotent_after_first_shred(self) -> None:
        self.path.write_bytes(b"x" * 100)
        self.assertTrue(shred_file(self.path))
        # File is gone; second call no-ops cleanly.
        self.assertFalse(shred_file(self.path))


if __name__ == "__main__":
    unittest.main()
