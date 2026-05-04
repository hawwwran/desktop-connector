"""T9.2 — Server endpoints for relay-to-relay migration.

Spins up a hermetic PHP server, creates a vault, then exercises:

- ``POST /api/vaults/{id}/migration/start``        (idempotent, returns token once)
- ``GET  /api/vaults/{id}/migration/verify-source`` (manifest hash + counts)
- ``PUT  /api/vaults/{id}/migration/commit``        (flips ``migrated_to``)

Plus the post-commit guard: a write to the now-source vault returns
409 ``vault_migration_in_progress`` per §H2.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

# Re-use the harness from the existing server contract tests.
from test_server_contract import _ServerHarness  # noqa: E402


VAULT_ID_DASHED = "MIGT-2345-WXYZ"
VAULT_ID_BARE = "MIGT2345WXYZ"


class ServerVaultMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ServerHarness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    # --- harness helpers --------------------------------------------------

    def _register_device(self) -> tuple[str, str]:
        public_key = base64.b64encode(os.urandom(32)).decode("ascii")
        status, _h, body = self.h.request(
            "POST",
            "/api/devices/register",
            json_body={"public_key": public_key, "device_type": "desktop"},
        )
        self.assertEqual(status, 201)
        return body["device_id"], body["auth_token"]

    def _create_vault(self, *, device_id: str, token: str, vault_secret: str) -> dict:
        secret_hash = hashlib.sha256(vault_secret.encode("ascii")).digest()
        status, _h, body = self.h.request(
            "POST", "/api/vaults",
            token=token, device_id=device_id,
            json_body={
                "vault_id": VAULT_ID_DASHED,
                "vault_access_token_hash": base64.b64encode(secret_hash).decode("ascii"),
                "encrypted_header": base64.b64encode(b"header-bytes-stub").decode("ascii"),
                "header_hash": "a" * 64,
                "initial_manifest_ciphertext": base64.b64encode(b"manifest-stub").decode("ascii"),
                "initial_manifest_hash": "b" * 64,
            },
        )
        self.assertEqual(status, 201, body)
        return body["data"]

    def _vault_request(
        self,
        method: str,
        path: str,
        *,
        device_id: str,
        device_token: str,
        vault_secret: str,
        body: dict | None = None,
    ) -> tuple[int, dict | str]:
        """Authenticated vault request (X-Device-Id + X-Vault-ID + X-Vault-Authorization)."""
        headers = {
            "Authorization": f"Bearer {device_token}",
            "X-Device-Id": device_id,
            "X-Vault-ID": VAULT_ID_BARE,
            "X-Vault-Authorization": f"Bearer {vault_secret}",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.h.base_url + path,
            method=method,
            headers=headers,
            data=data,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                payload = res.read().decode("utf-8")
                ct = res.headers.get("Content-Type", "")
                parsed = json.loads(payload) if "application/json" in ct else payload
                return res.status, parsed
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8")
            ct = e.headers.get("Content-Type", "")
            parsed = json.loads(payload) if "application/json" in ct and payload else payload
            return e.code, parsed

    # --- tests -----------------------------------------------------------

    def test_full_migration_lifecycle(self) -> None:
        device_id, device_token = self._register_device()
        vault_secret = "vault-mig-secret"
        self._create_vault(
            device_id=device_id, token=device_token, vault_secret=vault_secret,
        )
        target = "https://target.example.com/SERVICES/dc"

        # /start the first time creates the intent and emits a token.
        status, body = self._vault_request(
            "POST", f"/api/vaults/{VAULT_ID_BARE}/migration/start",
            device_id=device_id, device_token=device_token,
            vault_secret=vault_secret,
            body={"target_relay_url": target},
        )
        self.assertEqual(status, 201, body)
        self.assertTrue(body["data"]["token_returned"])
        self.assertTrue(body["data"]["token"].startswith("mig_v1_"))
        first_token = body["data"]["token"]

        # /start the second time with the same target is idempotent — no
        # token re-emission, 200 OK, target unchanged.
        status, body = self._vault_request(
            "POST", f"/api/vaults/{VAULT_ID_BARE}/migration/start",
            device_id=device_id, device_token=device_token,
            vault_secret=vault_secret,
            body={"target_relay_url": target},
        )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["data"]["token_returned"])
        self.assertIsNone(body["data"]["token"])

        # /start with a *different* target while one is in flight 409s.
        status, body = self._vault_request(
            "POST", f"/api/vaults/{VAULT_ID_BARE}/migration/start",
            device_id=device_id, device_token=device_token,
            vault_secret=vault_secret,
            body={"target_relay_url": "https://other.example.com"},
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "vault_migration_in_progress")

        # /verify-source returns the source's authoritative numbers.
        status, body = self._vault_request(
            "GET", f"/api/vaults/{VAULT_ID_BARE}/migration/verify-source",
            device_id=device_id, device_token=device_token,
            vault_secret=vault_secret,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["target_relay_url"], target)
        self.assertEqual(body["data"]["manifest_hash"], "b" * 64)
        self.assertEqual(body["data"]["chunk_count"], 0)
        self.assertEqual(body["data"]["used_ciphertext_bytes"], 0)

        # /commit flips migrated_to. Idempotent on the same target.
        status, body = self._vault_request(
            "PUT", f"/api/vaults/{VAULT_ID_BARE}/migration/commit",
            device_id=device_id, device_token=device_token,
            vault_secret=vault_secret,
            body={"target_relay_url": target},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["target_relay_url"], target)

        # Post-commit, the source is read-only — write attempts get 409
        # with vault_migration_in_progress per §H2.
        status, body = self._vault_request(
            "PUT", f"/api/vaults/{VAULT_ID_BARE}/manifest",
            device_id=device_id, device_token=device_token,
            vault_secret=vault_secret,
            body={
                "expected_current_revision": 1,
                "new_revision": 2,
                "parent_revision": 1,
                "manifest_hash": "c" * 64,
                "manifest_ciphertext": base64.b64encode(b"new-manifest").decode("ascii"),
            },
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "vault_migration_in_progress")

        # /header surfaces migrated_to so other devices can switch
        # without an explicit migration-notification endpoint (§H2).
        status, body = self._vault_request(
            "GET", f"/api/vaults/{VAULT_ID_BARE}/header",
            device_id=device_id, device_token=device_token,
            vault_secret=vault_secret,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["migrated_to"], target)

    def test_verify_source_without_intent_is_400(self) -> None:
        device_id, device_token = self._register_device()
        vault_secret = "vault-no-intent"
        # Use a different vault id than the lifecycle test so the harness's
        # shared DB doesn't bleed state between tests.
        global VAULT_ID_DASHED, VAULT_ID_BARE
        saved_dashed = VAULT_ID_DASHED
        saved_bare = VAULT_ID_BARE
        try:
            VAULT_ID_DASHED = "NOIN-2345-WXYZ"
            VAULT_ID_BARE = "NOIN2345WXYZ"
            self._create_vault(
                device_id=device_id, token=device_token, vault_secret=vault_secret,
            )
            status, body = self._vault_request(
                "GET", f"/api/vaults/{VAULT_ID_BARE}/migration/verify-source",
                device_id=device_id, device_token=device_token,
                vault_secret=vault_secret,
            )
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"]["code"], "vault_invalid_request")
        finally:
            VAULT_ID_DASHED = saved_dashed
            VAULT_ID_BARE = saved_bare


if __name__ == "__main__":
    unittest.main()
