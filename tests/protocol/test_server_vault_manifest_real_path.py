"""End-to-end ``PUT /api/vaults/{id}/manifest`` test through the real PHP front controller.

Guards against autoload / ``require_once`` gaps in ``server/public/index.php``.
A missing ``require_once`` for ``Crypto/VaultCrypto.php`` once made
``VaultController::putManifest`` throw a PHP fatal at the
``VaultCrypto::parseManifestEnvelopeHeader`` call — the relay returned an
HTML stack trace, the desktop failed to parse JSON, and the user saw
"Relay returned an invalid vault manifest publish response."

The mocked ``VaultHttpRelay`` shims that other vault tests use never load
``index.php``, so they couldn't have caught this. This test does.

The shared PHP server is started once per module via ``setUpModule`` and
torn down via ``tearDownModule`` (tabula-rasa each run).
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402
from _real_relay_server import get_shared_server  # noqa: E402

ensure_desktop_on_path()
from src.vault_crypto import build_manifest_envelope  # noqa: E402


VAULT_ID_DASHED = "MFRP-2345-WXYZ"
VAULT_ID_BARE = "MFRP2345WXYZ"


def setUpModule() -> None:
    get_shared_server().start()


def tearDownModule() -> None:
    get_shared_server().stop()


class ServerVaultManifestRealPathTests(unittest.TestCase):
    """One PHP process for the whole class; each test registers its own device."""

    @property
    def relay(self):
        return get_shared_server()

    # -- harness helpers ---------------------------------------------------

    def _register_device(self) -> tuple[str, str]:
        public_key = base64.b64encode(os.urandom(32)).decode("ascii")
        status, _h, body = self.relay.request(
            "POST",
            "/api/devices/register",
            json_body={"public_key": public_key, "device_type": "desktop"},
        )
        self.assertEqual(status, 201, body)
        return body["device_id"], body["auth_token"]

    def _create_vault(
        self,
        *,
        device_id: str,
        token: str,
        vault_secret: str,
        vault_id_dashed: str,
        vault_id_bare: str,
    ) -> str:
        """Create a vault with revision=1 and return the initial manifest hash."""
        secret_hash = hashlib.sha256(vault_secret.encode("ascii")).digest()
        # Header bytes are opaque to the relay; pad to satisfy the format-version
        # byte plus a minimal envelope shape.
        encrypted_header = b"\x01" + b"\x00" * 84
        # Initial manifest envelope with revision=1, parent=0. AEAD bytes are
        # opaque to the server (it never decrypts the initial manifest), but the
        # 61-byte deterministic prefix must parse cleanly if any later code path
        # validates it. The author_device_id must match X-Device-ID — the
        # server's manifest tamper-check rejects mismatches at PUT time.
        initial_envelope = build_manifest_envelope(
            vault_id=vault_id_bare,
            revision=1,
            parent_revision=0,
            author_device_id=device_id,
            nonce=b"\x00" * 24,
            aead_ciphertext_and_tag=b"\x00" * 32,
        )
        manifest_hash = hashlib.sha256(initial_envelope).hexdigest()
        header_hash = hashlib.sha256(encrypted_header).hexdigest()
        status, _h, body = self.relay.request(
            "POST",
            "/api/vaults",
            token=token,
            device_id=device_id,
            json_body={
                "vault_id": vault_id_dashed,
                "vault_access_token_hash": base64.b64encode(secret_hash).decode("ascii"),
                "encrypted_header": base64.b64encode(encrypted_header).decode("ascii"),
                "header_hash": header_hash,
                "initial_manifest_ciphertext": base64.b64encode(initial_envelope).decode("ascii"),
                "initial_manifest_hash": manifest_hash,
            },
        )
        self.assertEqual(status, 201, body)
        return manifest_hash

    def _vault_headers(self, *, token: str, device_id: str, vault_secret: str, vault_id_bare: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "X-Device-Id": device_id,
            "X-Vault-ID": vault_id_bare,
            "X-Vault-Authorization": f"Bearer {vault_secret}",
        }

    # -- tests -------------------------------------------------------------

    def test_put_manifest_happy_path_through_real_php(self) -> None:
        """Successful manifest publish must traverse every wired class.

        Regression: missing ``require_once`` for ``Crypto/VaultCrypto.php``
        in ``public/index.php`` caused ``VaultController::putManifest`` to
        throw ``Class "VaultCrypto" not found`` when reaching the envelope
        header parse. This test exercises that exact line.
        """
        vault_secret = "vault-mfrp-real-path"
        device_id, token = self._register_device()
        self._create_vault(
            device_id=device_id,
            token=token,
            vault_secret=vault_secret,
            vault_id_dashed=VAULT_ID_DASHED,
            vault_id_bare=VAULT_ID_BARE,
        )

        new_envelope = build_manifest_envelope(
            vault_id=VAULT_ID_BARE,
            revision=2,
            parent_revision=1,
            author_device_id=device_id,
            nonce=b"\x11" * 24,
            aead_ciphertext_and_tag=b"\x22" * 64,
        )
        new_manifest_hash = hashlib.sha256(new_envelope).hexdigest()

        status, _h, body = self.relay.request(
            "PUT",
            f"/api/vaults/{VAULT_ID_BARE}/manifest",
            headers=self._vault_headers(
                token=token,
                device_id=device_id,
                vault_secret=vault_secret,
                vault_id_bare=VAULT_ID_BARE,
            ),
            json_body={
                "expected_current_revision": 1,
                "new_revision": 2,
                "parent_revision": 1,
                "manifest_hash": new_manifest_hash,
                "manifest_ciphertext": base64.b64encode(new_envelope).decode("ascii"),
            },
        )

        # If VaultCrypto isn't loaded, PHP renders a fatal as HTML and the
        # body is a string starting with "<br />" or "<b>Fatal error</b>".
        # Surface that explicitly so the failure message is actionable.
        self.assertIsInstance(
            body, dict,
            f"relay returned non-JSON (HTTP {status}); first 200 chars: {str(body)[:200]!r}",
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        self.assertIsInstance(body.get("data"), dict, body)
        self.assertEqual(body["data"]["revision"], 2)
        self.assertEqual(body["data"]["manifest_hash"], new_manifest_hash)


if __name__ == "__main__":
    unittest.main()
