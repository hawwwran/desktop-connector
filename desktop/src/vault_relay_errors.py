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


class VaultCASConflictError(VaultRelayError):
    """Server rejected a manifest publish because the CAS revision moved (HTTP 409)."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error, status_code=409)
        self.current_revision = self.details.get("current_revision")
