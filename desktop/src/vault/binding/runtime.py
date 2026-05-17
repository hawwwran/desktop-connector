"""Runtime adapters for desktop Vault windows."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path


# F-D23: server error pages occasionally reflect POST-body fields. The
# desktop must never propagate purge_secret / Authorization / passphrase
# back into a user-facing message — even for transient 5xx errors that
# could later land in the activity log. Pattern targets the visible
# tokens we know we send.
_SECRET_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r'(?i)(purge_secret\s*[:=]\s*)\S+'), r"\1<redacted>"),
    (re.compile(r'(?i)(passphrase\s*[:=]\s*)\S+'), r"\1<redacted>"),
    (re.compile(r'(?i)(Authorization\s*:\s*)[^\s"\']+'), r"\1<redacted>"),
    (re.compile(r'(?i)(Bearer\s+)[A-Za-z0-9._\-]{16,}'), r"\1<redacted>"),
)


def _scrub_secrets(text: str) -> str:
    """Best-effort redaction of secret tokens before user-facing display."""
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


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
    from ..grant.store import VaultGrant, open_default_grant_store

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
    from .. import Vault
    from ..grant.store import open_default_grant_store

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
        from ...crypto import KeyManager

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
        from ...connection import ConnectionManager
        self._conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)

    def create_vault(
        self, vault_id, vault_access_token_hash, encrypted_header,
        header_hash,
        initial_root_ciphertext, initial_root_hash,
        *,
        initial_root_revision=None,
        initial_header_revision=None,
    ):
        payload = {
            "vault_id": vault_id,
            "vault_access_token_hash": base64.b64encode(vault_access_token_hash).decode("ascii"),
            "encrypted_header": base64.b64encode(encrypted_header).decode("ascii"),
            "header_hash": header_hash,
            "initial_root_ciphertext": base64.b64encode(initial_root_ciphertext).decode("ascii"),
            "initial_root_hash": initial_root_hash,
        }
        if initial_root_revision is not None:
            payload["initial_root_revision"] = int(initial_root_revision)
        if initial_header_revision is not None:
            payload["initial_header_revision"] = int(initial_header_revision)
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

    def put_header(self, vault_id, vault_access_secret, *,
                   expected_header_revision, new_header_revision,
                   encrypted_header, header_hash):
        """CAS-replace the encrypted header. Used by the resume flow to
        rotate recovery material after a cross-session orphan adoption.
        """
        payload = {
            "expected_header_revision": int(expected_header_revision),
            "new_header_revision": int(new_header_revision),
            "encrypted_header": base64.b64encode(encrypted_header).decode("ascii"),
            "header_hash": header_hash,
        }
        resp = self._conn.request(
            "PUT",
            f"/api/vaults/{vault_id}/header",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json=payload,
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while replacing the vault header.")
        if resp.status_code == 409:
            from ..relay_errors import VaultCASConflictError
            raise VaultCASConflictError(self._extract_error(resp))
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault header replace: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            return resp.json().get("data", {})
        except ValueError:
            return {}

    def get_header(self, vault_id, vault_access_secret):
        resp = self._conn.request(
            "GET",
            f"/api/vaults/{vault_id}/header",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while fetching the vault header.")
        if resp.status_code == 404:
            from ..relay_errors import VaultNotFoundError
            err = self._extract_error(resp)
            raise VaultNotFoundError(err.get("message") or "vault_not_found")
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

    def get_root(self, vault_id, vault_access_secret):
        resp = self._conn.request(
            "GET",
            f"/api/vaults/{vault_id}/root",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while fetching the vault root.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault root fetch: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            body = resp.json()
            data = body["data"]
            data["root_ciphertext"] = base64.b64decode(data["root_ciphertext"])
            return data
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid vault root response.") from exc

    def put_root(
        self,
        vault_id,
        vault_access_secret,
        *,
        expected_current_root_revision,
        new_root_revision,
        parent_root_revision,
        root_hash,
        root_ciphertext,
    ):
        payload = {
            "expected_current_root_revision": int(expected_current_root_revision),
            "new_root_revision": int(new_root_revision),
            "parent_root_revision": int(parent_root_revision),
            "root_hash": root_hash,
            "root_ciphertext": base64.b64encode(root_ciphertext).decode("ascii"),
        }
        resp = self._conn.request(
            "PUT",
            f"/api/vaults/{vault_id}/root",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json=payload,
        )
        return self._handle_manifest_like_publish(resp, kind="root")

    def get_shard(self, vault_id, vault_access_secret, remote_folder_id):
        resp = self._conn.request(
            "GET",
            f"/api/vaults/{vault_id}/folders/{remote_folder_id}/shard",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while fetching a folder shard.")
        if resp.status_code == 404:
            from ..relay_errors import VaultNotFoundError
            err = self._extract_error(resp)
            raise VaultNotFoundError(err.get("message") or f"shard {remote_folder_id} not found")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected folder shard fetch: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            body = resp.json()
            data = body["data"]
            data["shard_ciphertext"] = base64.b64decode(data["shard_ciphertext"])
            return data
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid folder shard response.") from exc

    def put_shard(
        self,
        vault_id,
        vault_access_secret,
        remote_folder_id,
        *,
        expected_current_shard_revision,
        new_shard_revision,
        parent_shard_revision,
        shard_hash,
        shard_ciphertext,
    ):
        payload = {
            "expected_current_shard_revision": int(expected_current_shard_revision),
            "new_shard_revision": int(new_shard_revision),
            "parent_shard_revision": int(parent_shard_revision),
            "shard_hash": shard_hash,
            "shard_ciphertext": base64.b64encode(shard_ciphertext).decode("ascii"),
        }
        resp = self._conn.request(
            "PUT",
            f"/api/vaults/{vault_id}/folders/{remote_folder_id}/shard",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json=payload,
        )
        return self._handle_manifest_like_publish(resp, kind="shard")

    def put_shard_with_root(
        self,
        vault_id,
        vault_access_secret,
        remote_folder_id,
        *,
        shard,
        root,
    ):
        payload = {
            "shard": {
                "expected_current_shard_revision": int(shard["expected_current_shard_revision"]),
                "new_shard_revision":              int(shard["new_shard_revision"]),
                "parent_shard_revision":           int(shard["parent_shard_revision"]),
                "shard_hash":                      shard["shard_hash"],
                "shard_ciphertext":                base64.b64encode(shard["shard_ciphertext"]).decode("ascii"),
            },
            "root": {
                "expected_current_root_revision": int(root["expected_current_root_revision"]),
                "new_root_revision":              int(root["new_root_revision"]),
                "parent_root_revision":           int(root["parent_root_revision"]),
                "root_hash":                      root["root_hash"],
                "root_ciphertext":                base64.b64encode(root["root_ciphertext"]).decode("ascii"),
            },
        }
        resp = self._conn.request(
            "PUT",
            f"/api/vaults/{vault_id}/folders/{remote_folder_id}/shard-with-root",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json=payload,
        )
        return self._handle_manifest_like_publish(resp, kind="shard-with-root")

    def _handle_manifest_like_publish(self, resp, *, kind: str):
        """Shared error mapping for the three CAS-publish endpoints
        (root / shard / shard-with-root). Mirrors the legacy
        ``put_manifest`` handling: 409 → VaultCASConflictError,
        507 → VaultQuotaExceededError, 413/422 → VaultRelayError,
        anything else → RuntimeError. Returns the success body's
        ``data`` dict.
        """
        if resp is None:
            raise RuntimeError(
                f"Could not reach the relay while publishing the vault {kind}.",
            )
        if resp.status_code == 409:
            from ..relay_errors import VaultCASConflictError
            raise VaultCASConflictError(self._extract_error(resp))
        if resp.status_code == 507:
            from ..relay_errors import VaultQuotaExceededError
            raise VaultQuotaExceededError(self._extract_error(resp))
        if resp.status_code in (413, 422):
            from ..relay_errors import VaultRelayError
            raise VaultRelayError(
                self._extract_error(resp), status_code=resp.status_code,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault {kind} publish: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            from ..relay_errors import VaultRelayUnexpectedResponseError
            raise VaultRelayUnexpectedResponseError(
                f"Relay returned a non-JSON vault {kind} publish response.",
                status_code=resp.status_code,
                response_text=_scrub_secrets(resp.text or ""),
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            from ..relay_errors import VaultRelayUnexpectedResponseError
            raise VaultRelayUnexpectedResponseError(
                f"Relay returned an invalid vault {kind} publish response.",
                status_code=resp.status_code,
                response_text=_scrub_secrets(resp.text or ""),
            )
        return body["data"]

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
        if resp.status_code == 404:
            # F-D27: typed error so the download retry budget can
            # distinguish "chunk not yet uploaded by peer" from a
            # generic relay failure.
            from ..relay_errors import VaultChunkMissingError

            raise VaultChunkMissingError(
                f"vault chunk missing: {chunk_id}",
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault chunk download: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        return resp.content

    def migration_start(self, vault_id, vault_access_secret, *, target_relay_url):
        """T9.2 — record migration intent on the source relay."""
        resp = self._conn.request(
            "POST",
            f"/api/vaults/{vault_id}/migration/start",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json={"target_relay_url": str(target_relay_url)},
        )
        if resp is None:
            raise RuntimeError("Could not reach the source relay while starting migration.")
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Relay rejected migration start: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            return resp.json()["data"]
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid migration-start response.") from exc

    def migration_verify_source(self, vault_id, vault_access_secret):
        """T9.2 — fetch the source's authoritative aggregates for diffing."""
        resp = self._conn.request(
            "GET",
            f"/api/vaults/{vault_id}/migration/verify-source",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
        )
        if resp is None:
            raise RuntimeError("Could not reach the source relay while verifying migration.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected migration verify: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            return resp.json()["data"]
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid migration-verify response.") from exc

    def migration_commit(self, vault_id, vault_access_secret, *, target_relay_url):
        """T9.2 — flip the source vault to read-only, stamping migrated_to."""
        resp = self._conn.request(
            "PUT",
            f"/api/vaults/{vault_id}/migration/commit",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json={"target_relay_url": str(target_relay_url)},
        )
        if resp is None:
            raise RuntimeError("Could not reach the source relay while committing migration.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected migration commit: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            return resp.json()["data"]
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid migration-commit response.") from exc

    def gc_plan(
        self,
        vault_id,
        vault_access_secret,
        *,
        root_revision,
        candidate_chunk_ids,
        purpose="sync",
    ):
        # Review §3.C1: ``purpose='forced_eviction'`` flags an eviction
        # stage 2/3 plan (unexpired tombstones or historical versions),
        # which the relay then gates behind role=admin on /gc/execute.
        body = {
            "root_revision": int(root_revision),
            "candidate_chunk_ids": list(candidate_chunk_ids),
        }
        if purpose != "sync":
            body["purpose"] = str(purpose)
        resp = self._conn.request(
            "POST",
            f"/api/vaults/{vault_id}/gc/plan",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json=body,
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

    def gc_cancel(self, vault_id, vault_access_secret, *, plan_id=None, job_id=None):
        """Cancel a planned/scheduled GC job (F-D06).

        Either ``plan_id`` or ``job_id`` must be provided — see spec
        §6.14. Idempotent: cancelling an already-cancelled or
        already-completed job is a 200 with no state change.
        """
        if plan_id is None and job_id is None:
            raise ValueError("gc_cancel requires plan_id or job_id")
        body: dict = {}
        if plan_id is not None:
            body["plan_id"] = str(plan_id)
        if job_id is not None:
            body["job_id"] = str(job_id)
        resp = self._conn.request(
            "POST",
            f"/api/vaults/{vault_id}/gc/cancel",
            headers={"X-Vault-Authorization": f"Bearer {vault_access_secret}"},
            json=body,
        )
        if resp is None:
            raise RuntimeError("Could not reach the relay while cancelling vault GC.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Relay rejected vault GC cancel: HTTP {resp.status_code} "
                f"{self._error_message(resp)}"
            )
        try:
            return resp.json()["data"]
        except Exception as exc:
            raise RuntimeError("Relay returned an invalid GC cancel response.") from exc

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
        from ..relay_errors import VaultQuotaExceededError, VaultRelayError

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
            return {
                "code": "",
                "message": _scrub_secrets(resp.text.strip()[:200]),
                "details": {},
            }
        if not isinstance(body, dict):
            return {"code": "", "message": "", "details": {}}
        error = body.get("error")
        if not isinstance(error, dict):
            return {"code": "", "message": _scrub_secrets(str(error or "")), "details": {}}
        return {
            "code": str(error.get("code") or ""),
            "message": _scrub_secrets(str(error.get("message") or "")),
            "details": error.get("details") if isinstance(error.get("details"), dict) else {},
        }

    @staticmethod
    def _error_message(resp) -> str:
        try:
            body = resp.json()
        except ValueError:
            return _scrub_secrets(resp.text.strip()[:200])
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                if code and message:
                    return _scrub_secrets(f"{code}: {message}")
                if message:
                    return _scrub_secrets(str(message))
            if isinstance(error, str):
                return _scrub_secrets(error)
        return ""


class VaultLocalDevelopmentRelay:
    """Explicit opt-in local relay for GUI smoke tests without a server."""

    def __init__(self, config) -> None:
        self._config = config

    def create_vault(self, vault_id, vault_access_token_hash, encrypted_header,
                     header_hash, initial_root_ciphertext=None, initial_root_hash=None,
                     **kwargs):
        from .. import crypto as vault_crypto  # noqa: F401
        return {"vault_id": vault_id}

    def get_header(self, vault_id, vault_access_secret):
        raise NotImplementedError("local development relay does not support header fetch")

    def get_root(self, vault_id, vault_access_secret):
        raise NotImplementedError("local development relay does not support root fetch")

    def put_root(self, vault_id, vault_access_secret, **kwargs):
        raise NotImplementedError("local development relay does not support root publish")

    def get_shard(self, vault_id, vault_access_secret, remote_folder_id):
        raise NotImplementedError("local development relay does not support shard fetch")

    def put_shard(self, vault_id, vault_access_secret, remote_folder_id, **kwargs):
        raise NotImplementedError("local development relay does not support shard publish")

    def put_shard_with_root(self, vault_id, vault_access_secret, remote_folder_id, **kwargs):
        raise NotImplementedError("local development relay does not support shard-with-root publish")

    def batch_head_chunks(self, vault_id, vault_access_secret, chunk_ids):
        raise NotImplementedError("local development relay does not support chunk checks")

    def get_chunk(self, vault_id, vault_access_secret, chunk_id):
        raise NotImplementedError("local development relay does not support chunk download")

    def put_chunk(self, vault_id, vault_access_secret, chunk_id, body):
        raise NotImplementedError("local development relay does not support chunk upload")
