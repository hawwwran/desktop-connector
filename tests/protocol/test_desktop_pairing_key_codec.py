"""Codec tests for desktop-to-desktop pairing keys (M.11)."""

from __future__ import annotations

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.pairing_key import (  # noqa: E402
    PAIRING_KEY_FILE_EXT,
    PAIRING_KEY_PREFIX,
    PairingKey,
    PairingKeyParseError,
    PairingKeySchemaError,
    decode,
    default_filename,
    encode,
)


class _Sample:
    @staticmethod
    def key(name: str = "Workstation") -> PairingKey:
        return PairingKey(
            server="https://relay.example.com/SERVICES/desktop-connector",
            device_id="abc123def456abc123def456abc12345",
            pubkey="bGVnaXRpbWF0ZS1iYXNlNjQtcHVia2V5LWJsb2I=",
            name=name,
        )


class PairingKeyEncodeDecodeTests(unittest.TestCase):
    def test_round_trip_preserves_fields(self) -> None:
        original = _Sample.key()
        text = encode(original)
        self.assertTrue(text.startswith(PAIRING_KEY_PREFIX))
        decoded = decode(text)
        self.assertEqual(decoded, original)

    def test_encoded_form_uses_url_safe_base64(self) -> None:
        text = encode(_Sample.key("Tablet ☕"))
        # Strip the prefix; the rest must be URL-safe base64 (no '+' or '/').
        body = text[len(PAIRING_KEY_PREFIX):]
        self.assertNotIn("+", body)
        self.assertNotIn("/", body)
        # No padding on the encoded form (we strip it during encode).
        self.assertNotIn("=", body)

    def test_decode_strips_whitespace_and_newlines(self) -> None:
        # Soft-wrap from chat — newlines mid-blob.
        text = encode(_Sample.key())
        wrapped = text[:30] + "\n " + text[30:60] + "\n\t" + text[60:]
        self.assertEqual(decode(wrapped), _Sample.key())

    def test_decode_tolerates_missing_prefix(self) -> None:
        # Defensive paste UX: bare base64 is accepted too.
        text = encode(_Sample.key())
        bare = text[len(PAIRING_KEY_PREFIX):]
        self.assertEqual(decode(bare), _Sample.key())

    def test_decode_tolerates_extra_padding(self) -> None:
        text = encode(_Sample.key())
        # Add "=" padding back; encoder strips it but decoder must accept.
        padded = text + "=="
        self.assertEqual(decode(padded), _Sample.key())

    def test_decode_rejects_empty(self) -> None:
        with self.assertRaises(PairingKeyParseError):
            decode("   \n\t ")

    def test_decode_rejects_invalid_base64(self) -> None:
        with self.assertRaises(PairingKeyParseError):
            decode(f"{PAIRING_KEY_PREFIX}!!!not-base64!!!")

    def test_decode_rejects_malformed_json(self) -> None:
        body = base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode("ascii")
        with self.assertRaises(PairingKeyParseError):
            decode(f"{PAIRING_KEY_PREFIX}{body}")

    def test_decode_rejects_non_object_json(self) -> None:
        body = base64.urlsafe_b64encode(b'["array", "not", "object"]').rstrip(b"=").decode("ascii")
        with self.assertRaises(PairingKeySchemaError):
            decode(f"{PAIRING_KEY_PREFIX}{body}")

    def test_decode_rejects_missing_field(self) -> None:
        partial = {"server": "https://x", "device_id": "d", "pubkey": "p"}
        body = base64.urlsafe_b64encode(
            json.dumps(partial).encode()
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(PairingKeySchemaError) as ctx:
            decode(f"{PAIRING_KEY_PREFIX}{body}")
        self.assertIn("name", str(ctx.exception))

    def test_decode_rejects_wrong_typed_field(self) -> None:
        bad = {
            "server": "https://x",
            "device_id": "d",
            "pubkey": "p",
            "name": 12345,  # int, not str
        }
        body = base64.urlsafe_b64encode(
            json.dumps(bad).encode()
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(PairingKeySchemaError):
            decode(f"{PAIRING_KEY_PREFIX}{body}")

    def test_decode_rejects_empty_required_field(self) -> None:
        bad = {
            "server": "https://x",
            "device_id": "d",
            "pubkey": "p",
            "name": "   ",  # whitespace-only counts as empty after strip
        }
        body = base64.urlsafe_b64encode(
            json.dumps(bad).encode()
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(PairingKeySchemaError):
            decode(f"{PAIRING_KEY_PREFIX}{body}")

    def test_decode_strips_internal_field_whitespace(self) -> None:
        # Trailing whitespace inside fields is trimmed by decode.
        original = _Sample.key()
        bad = {
            "server": original.server + "  ",
            "device_id": "  " + original.device_id,
            "pubkey": original.pubkey,
            "name": original.name,
        }
        body = base64.urlsafe_b64encode(
            json.dumps(bad).encode()
        ).rstrip(b"=").decode("ascii")
        decoded = decode(f"{PAIRING_KEY_PREFIX}{body}")
        self.assertEqual(decoded, original)


class DefaultFilenameTests(unittest.TestCase):
    def test_default_filename_uses_dcpair_extension(self) -> None:
        self.assertTrue(
            default_filename(_Sample.key("Workstation")).endswith(
                PAIRING_KEY_FILE_EXT
            )
        )

    def test_default_filename_sanitises_unsafe_chars(self) -> None:
        key = PairingKey(
            server="https://x",
            device_id="d" * 16,
            pubkey="p",
            name="My / Crazy \\ Tablet ?",
        )
        # Slashes, backslashes, and other shell-special chars become "-".
        # The result should still be readable and end with .dcpair.
        out = default_filename(key)
        self.assertTrue(out.endswith(PAIRING_KEY_FILE_EXT))
        self.assertNotIn("/", out)
        self.assertNotIn("\\", out)
        self.assertNotIn("?", out)
        self.assertIn("Tablet", out)

    def test_default_filename_falls_back_to_device(self) -> None:
        key = PairingKey(server="x", device_id="d", pubkey="p", name="???")
        out = default_filename(key)
        self.assertEqual(out, f"device{PAIRING_KEY_FILE_EXT}")


if __name__ == "__main__":
    unittest.main()
