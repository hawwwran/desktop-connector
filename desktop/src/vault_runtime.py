"""Runtime adapters for desktop Vault windows."""

from __future__ import annotations

import base64
import os
from pathlib import Path


def create_vault_relay(config):
    """Return the production relay by default.

    Local-only creation is kept for explicit development smoke tests.
    It must never be the implicit path, because it creates a vault marker
    that cannot sync or appear on the relay dashboard.
    """
    if os.environ.get("DESKTOP_CONNECTOR_VAULT_LOCAL_RELAY") == "1":
        return VaultLocalDevelopmentRelay(config)
    return VaultHttpRelay(config)


def save_local_vault_grant(config_dir: Path, config, vault) -> None:
    """Persist the creating device's local vault unlock grant."""
    from .vault_grant import VaultGrant, open_default_grant_store

    master_key = vault.master_key
    vault_access_secret = vault.vault_access_secret
    if master_key is None or vault_access_secret is None:
        raise RuntimeError("Vault material was closed before the local grant could be saved.")

    store = open_default_grant_store(
        config_dir=Path(config_dir),
        device_seed_provider=_vault_device_seed_provider(Path(config_dir), config),
    )
    grant = VaultGrant.from_bytes(vault.vault_id, master_key, vault_access_secret)
    try:
        store.save(grant)
    finally:
        grant.zero()


def open_local_vault_from_grant(config_dir: Path, config, vault_id: str):
    """Open vault state from this machine's saved grant."""
    from .vault import Vault
    from .vault_grant import open_default_grant_store

    store = open_default_grant_store(
        config_dir=Path(config_dir),
        device_seed_provider=_vault_device_seed_provider(Path(config_dir), config),
    )
    grant = store.load(vault_id)
    if grant is None:
        raise RuntimeError(
            "This vault is locked on this machine. Reopen or import the vault "
            "before adding folders."
        )
    try:
        return Vault.from_grant(grant)
    finally:
        grant.zero()


def _vault_device_seed_provider(config_dir: Path, config):
    """Return the fallback grant-store seed provider for this device."""
    def provider() -> bytes:
        from cryptography.hazmat.primitives import serialization
        from .crypto import KeyManager

        key_manager = KeyManager(Path(config_dir), secret_store=config.secret_store)
        return key_manager.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    return provider


class VaultHttpRelay:
    """Adapter from :class:`Vault`'s narrow relay protocol to HTTP."""

    def __init__(self, config) -> None:
        self._config = config
        config.reload()
        if not config.device_id or not config.auth_token:
            raise RuntimeError("Desktop Connector is not registered with the relay.")
        from .connection import ConnectionManager
        self._conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)

    def create_vault(self, vault_id, vault_access_token_hash, encrypted_header,
                     header_hash, initial_manifest_ciphertext, initial_manifest_hash):
        payload = {
            "vault_id": vault_id,
            "vault_access_token_hash": base64.b64encode(vault_access_token_hash).decode("ascii"),
            "encrypted_header": base64.b64encode(encrypted_header).decode("ascii"),
            "header_hash": header_hash,
            "initial_manifest_ciphertext": base64.b64encode(initial_manifest_ciphertext).decode("ascii"),
            "initial_manifest_hash": initial_manifest_hash,
        }
        resp = self._conn.request("POST", "/api/vaults", json=payload)
        if resp is None:
            raise RuntimeError("Could not reach the relay while creating the vault.")
        if resp.status_code != 201:
            raise RuntimeError(
                f"Relay rejected vault creation: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError("Relay returned a non-JSON vault creation response.") from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise RuntimeError("Relay returned an invalid vault creation response.")
        return body["data"]

    def get_header(self, vault_id, vault_access_secret):
        resp = self._conn.request(
            "GET",
            f"/api/vaults/{vault_id}/header",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while fetching the vault header.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault header fetch: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            body = resp.json()
            data = body["data"]
            data["encrypted_header"] = base64.b64decode(data["encrypted_header"])
            return data
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid vault header response.") from exc

    def get_manifest(self, vault_id, vault_access_secret):
        resp = self._conn.request(
            "GET",
            f"/api/vaults/{vault_id}/manifest",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while fetching the vault manifest.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault manifest fetch: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            body = resp.json()
            data = body["data"]
            data["manifest_ciphertext"] = base64.b64decode(data["manifest_ciphertext"])
            return data
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid vault manifest response.") from exc

    def put_manifest(
        self,
        vault_id,
        vault_access_secret,
        *,
        expected_current_revision,
        new_revision,
        parent_revision,
        manifest_hash,
        manifest_ciphertext,
    ):
        payload = {
            "expected_current_revision": int(expected_current_revision),
            "new_revision": int(new_revision),
            "parent_revision": int(parent_revision),
            "manifest_hash": manifest_hash,
            "manifest_ciphertext": base64.b64encode(manifest_ciphertext).decode("ascii"),
        }
        resp = self._conn.request(
            "PUT",
            f"/api/vaults/{vault_id}/manifest",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json=payload,
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while publishing the vault manifest.")
        if resp.status_code == 409:
            from .vault_relay_errors import VaultCASConflictError

            raise VaultCASConflictError(self._extract_error(resp))
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault manifest publish: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            body = resp.json()
            data = body["data"]
            return data
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid vault manifest publish response.") from exc

    def batch_head_chunks(self, vault_id, vault_access_secret, chunk_ids):
        chunks = {}
        ids = list(chunk_ids)
        for start in range(0, len(ids), 1024):
            batch = ids[start:start + 1024]
            resp = self._conn.request(
                "POST",
                f"/api/vaults/{vault_id}/chunks/batch-head",
                headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
                json={"chunk_ids": batch},
            )
            if resp is None:
                raise RuntimeError("Could not reach the relay while checking vault chunks.")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Relay rejected vault chunk check: HTTP {resp.status_code} "
                    f"{self._error_message(resp)}"
                )
            try:
                body = resp.json()
                data = body["data"]
                chunks.update(data["chunks"])
            except Exception as exc:
                raise RuntimeError("Relay returned an invalid vault chunk check response.") from exc
        return chunks

    def get_chunk(self, vault_id, vault_access_secret, chunk_id):
        resp = self._conn.request(
            "GET",
            f"/api/vaults/{vault_id}/chunks/{chunk_id}",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while downloading a vault chunk.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault chunk download: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        return resp.content

    def gc_plan(self, vault_id, vault_access_secret, *, manifest_revision, candidate_chunk_ids):
        resp = self._conn.request(
            "POST",
            f"/api/vaults/{vault_id}/gc/plan",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json={
                "manifest_revision": int(manifest_revision),
                "candidate_chunk_ids": list(candidate_chunk_ids),
            },
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while planning vault GC.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault GC plan: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            return resp.json()["data"]
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid GC plan response.") from exc

    def gc_execute(self, vault_id, vault_access_secret, *, plan_id, purge_secret=None):
        body = {"plan_id": str(plan_id)}
        if purge_secret is not None:
            body["purge_secret"] = str(purge_secret)
        resp = self._conn.request(
            "POST",
            f"/api/vaults/{vault_id}/gc/execute",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json=body,
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while executing vault GC.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault GC execute: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            return resp.json()["data"]
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid GC execute response.") from exc

    def put_chunk(self, vault_id, vault_access_secret, chunk_id, body):
        from .vault_relay_errors import VaultQuotaExceededError, VaultRelayError

        resp = self._conn.request(
            "PUT",
            f"/api/vaults/{vault_id}/chunks/{chunk_id}",
            headers={
                "X-Vault-Authorization": f"Bearer {vault_access_secret}",
                "Content-Type": "application/octet-stream",
            },
            data=bytes(body),
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while uploading a vault chunk.")
        if resp.status_code == 201:
            return {"created": True, "stored_size": len(body)}
        if resp.status_code == 200:
            return {"created": False, "stored_size": len(body)}
        if resp.status_code == 507:
            raise VaultQuotaExceededError(self._extract_error(resp))
        raise VaultRelayError(
            self._extract_error(resp),
            status_code=resp.status_code,
        )

    @staticmethod
    def _extract_error(resp) -> dict:
        try:
            body = resp.json()
        except ValueError:
            return {"code": "", "message": resp.text.strip()[:200], "details": {}}
        if not isinstance(body, dict):
            return {"code": "", "message": "", "details": {}}
        error = body.get("error")
        if not isinstance(error, dict):
            return {"code": "", "message": str(error or ""), "details": {}}
        return {
            "code": str(error.get("code") or ""),
            "message": str(error.get("message") or ""),
            "details": error.get("details") if isinstance(error.get("details"), dict) else {},
        }

    @staticmethod
    def _error_message(resp) -> str:
        try:
            body = resp.json()
        except ValueError:
            return resp.text.strip()[:200]
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                if code and message:
                    return f"{code}: {message}"
                if message:
                    return str(message)
            if isinstance(error, str):
                return error
        return ""


class VaultLocalDevelopmentRelay:
    """Explicit opt-in local relay for GUI smoke tests without a server."""

    def __init__(self, config) -> None:
        self._config = config

    def create_vault(self, vault_id, vault_access_token_hash, encrypted_header,
                     header_hash, initial_manifest_ciphertext, initial_manifest_hash):
        from . import vault_crypto  # noqa: F401
        return {"vault_id": vault_id}

    def get_header(self, vault_id, vault_access_secret):
        raise NotImplementedError("local development relay does not support header fetch")

    def get_manifest(self, vault_id, vault_access_secret):
        raise NotImplementedError("local development relay does not support manifest fetch")

    def put_manifest(self, vault_id, vault_access_secret, **kwargs):
        raise NotImplementedError("local development relay does not support manifest publish")

    def batch_head_chunks(self, vault_id, vault_access_secret, chunk_ids):
        raise NotImplementedError("local development relay does not support chunk checks")

    def get_chunk(self, vault_id, vault_access_secret, chunk_id):
        raise NotImplementedError("local development relay does not support chunk download")

    def put_chunk(self, vault_id, vault_access_secret, chunk_id, body):
        raise NotImplementedError("local development relay does not support chunk upload")
