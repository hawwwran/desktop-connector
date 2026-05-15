"""Typed errors raised by the vault relay client.

Lives in its own module so :mod:`desktop.src.vault_runtime`,
:mod:`desktop.src.vault_upload` and tests can import these without a
circular dependency through ``Vault``.
"""

from __future__ import annotations

from typing import Any


class VaultRelayError(RuntimeError):
    """Server returned an HTTP error the upload pipeline cares about."""

    def __init__(self, error: dict[str, Any], *, status_code: int) -> None:
        self.code = str(error.get("code") or "")
        self.message = str(error.get("message") or "")
        self.details = dict(error.get("details") or {})
        self.status_code = int(status_code)
        super().__init__(
            f"vault relay HTTP {self.status_code}: {self.code or 'error'}"
            + (f": {self.message}" if self.message else "")
        )


class VaultQuotaExceededError(VaultRelayError):
    """Server reported ``vault_quota_exceeded`` (HTTP 507)."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error, status_code=507)
        details = self.details
        self.used_bytes = int(details.get("used_ciphertext_bytes") or 0)
        self.quota_bytes = int(details.get("quota_ciphertext_bytes") or 0)
        self.eviction_available = bool(details.get("eviction_available", False))


class VaultChunkMissingError(VaultRelayError):
    """Server returned 404 ``vault_chunk_missing`` for a chunk fetch.

    Auto-retried by the download pipeline within the spec's transfer
    budget; surfaces as terminal once the budget is exhausted.
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            {"code": "vault_chunk_missing", "message": message},
            status_code=404,
        )


class VaultNotFoundError(VaultRelayError):
    """Server returned 404 ``vault_not_found`` for a vault-id-scoped call.

    Distinct from :class:`VaultChunkMissingError` so the resume worker
    can distinguish "the orphan row is still there" from "the row is
    gone, publish a fresh one under the same id". Substring-matching the
    HTTP error message would be fragile: a future relay-adapter cleanup
    that touches the message format could silently flip a 5xx into a
    fake 404 and cause a duplicate POST.
    """

    def __init__(self, message: str = "vault_not_found") -> None:
        super().__init__(
            {"code": "vault_not_found", "message": message},
            status_code=404,
        )


class VaultRelayUnexpectedResponseError(RuntimeError):
    """Relay returned HTTP 2xx but the body shape was not what the client expected.

    Carries the full response text so the UI can offer a "Show details"
    button next to the user-facing error.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        response_text: str,
    ) -> None:
        self.status_code = int(status_code)
        self.response_text = str(response_text or "")
        super().__init__(message)


class VaultManifestRollbackError(RuntimeError):
    """Relay served a manifest revision older than this device has seen.

    Raised by :meth:`Vault.decrypt_manifest` when the AEAD-verified
    revision is strictly less than the per-vault floor persisted in
    :class:`VaultLocalIndex`. Per the §3.7 risk evaluation
    (`docs/vault-critical-risks-evaluation.md`) the served manifest
    is **not** auto-applied — the local folder cache stays at the
    last-good revision so callers can surface a banner and offer
    the user a chance to investigate (integrity check, export
    restore, fresh re-pair) before trusting the relay again.

    Carries enough context (``vault_id``, ``served_revision``,
    ``floor_revision``) for the UI banner copy and the
    ``vault.manifest.rollback_detected`` diagnostic event.
    """

    def __init__(
        self,
        *,
        vault_id: str,
        served_revision: int,
        floor_revision: int,
    ) -> None:
        self.vault_id = str(vault_id)
        self.served_revision = int(served_revision)
        self.floor_revision = int(floor_revision)
        super().__init__(
            f"vault relay served manifest revision {self.served_revision} "
            f"but device has previously seen revision {self.floor_revision}"
        )


class VaultCASConflictError(VaultRelayError):
    """Server rejected a manifest publish because the CAS revision moved (HTTP 409).

    Per §A1, the server's 409 body returns ``current_revision``,
    ``current_manifest_hash``, ``current_manifest_ciphertext`` (base64)
    and ``current_manifest_size`` so the client can run §D4 merge in a
    single round-trip without a follow-up GET.
    """

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error, status_code=409)
        details = self.details
        self.current_revision = details.get("current_revision")
        self.current_manifest_hash = str(details.get("current_manifest_hash") or "")
        self.current_manifest_ciphertext_b64 = str(
            details.get("current_manifest_ciphertext") or ""
        )
        try:
            self.current_manifest_size = int(details.get("current_manifest_size") or 0)
        except (TypeError, ValueError):
            self.current_manifest_size = 0

    def current_manifest_ciphertext_bytes(self) -> bytes:
        """Decoded server-head manifest envelope, ready for ``decrypt_manifest``."""
        import base64

        if not self.current_manifest_ciphertext_b64:
            return b""
        return base64.b64decode(self.current_manifest_ciphertext_b64)
