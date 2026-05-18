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


class FreshUnlockRequiredError(RuntimeError):
    """Sensitive vault operation attempted without a fresh-unlock stamp.

    Raised by :func:`desktop.src.vault.fresh_unlock.require_fresh_unlock`
    at the entry to clear-folder / clear-vault / schedule-purge /
    import-merge handlers (per the §3.9 / §3.11 risk evaluation and
    architecture doc §13 — sensitive operations always require fresh
    unlock regardless of the unlock timeout setting). The caller
    handles this by surfacing the inline "Unlock with recovery
    passphrase to continue" mini-prompt and re-trying the operation
    on successful re-verification; the exception is the typed carrier
    the gate tests assert on.

    ``operation`` carries a short label (e.g. ``"clear-folder"``) so
    diagnostic logs can attribute the deny to the specific gate site.
    """

    def __init__(self, *, operation: str = "") -> None:
        self.operation = str(operation or "")
        msg = "fresh-unlock required"
        if self.operation:
            msg = f"fresh-unlock required for {self.operation!r}"
        super().__init__(msg)


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


class VaultShardHashMismatchError(RuntimeError):
    """A fetched shard envelope's hash disagrees with the trusted root pointer.

    Raised when a §10.C hash-chain check fails: the relay returned a
    shard envelope whose ``sha256(envelope_bytes)`` does not match the
    ``shard_hash`` recorded in the freshly-fetched (AEAD-verified) root
    pointer for that ``remote_folder_id``. AEAD on the shard itself
    succeeds in that scenario because the bytes are an authentic prior
    shard — what fails is the cross-envelope consistency the root
    pledges. Caught at decrypt time so the per-folder rollback is
    surfaced before any plaintext shard entries are consumed.

    Same trust shape as :class:`VaultManifestRollbackError` for the
    root revision floor; the two together close the rollback window
    on each axis of the sharded manifest.
    """

    def __init__(
        self,
        *,
        vault_id: str,
        remote_folder_id: str,
        expected_shard_hash: str,
        actual_shard_hash: str,
    ) -> None:
        self.vault_id = str(vault_id)
        self.remote_folder_id = str(remote_folder_id)
        self.expected_shard_hash = str(expected_shard_hash)
        self.actual_shard_hash = str(actual_shard_hash)
        super().__init__(
            f"vault relay served a shard for {self.remote_folder_id} whose "
            f"sha256 ({self.actual_shard_hash!r}) does not match the trusted "
            f"root pointer's shard_hash ({self.expected_shard_hash!r})"
        )


class VaultCASConflictError(VaultRelayError):
    """Server rejected a publish because the CAS revision moved (HTTP 409).

    Covers three Phase-B-and-later conflict shapes plus the legacy
    single-manifest shape (still emitted by the Phase D ``put_manifest``
    compat shim).

    Phase B/D shapes (after sharding):

      * ``vault_root_conflict``       — ``current_root_*`` fields.
      * ``vault_shard_conflict``      — ``current_shard_*`` fields +
                                        ``remote_folder_id``.
      * ``vault_shard_root_conflict`` — both sets of fields (atomic
                                        publish, both sides drifted).

    Legacy single-manifest shape:

      * ``vault_manifest_conflict`` — ``current_revision`` +
        ``current_manifest_*`` fields. Used during the Phase H transition
        only.
    """

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error, status_code=409)
        details = self.details
        # Legacy single-manifest payload (Phase H removal target).
        self.current_revision = details.get("current_revision")
        self.current_manifest_hash = str(details.get("current_manifest_hash") or "")
        self.current_manifest_ciphertext_b64 = str(
            details.get("current_manifest_ciphertext") or ""
        )
        try:
            self.current_manifest_size = int(details.get("current_manifest_size") or 0)
        except (TypeError, ValueError):
            self.current_manifest_size = 0
        # Phase B sharded payloads. Either set may be absent; callers
        # check the matching ciphertext field to know which.
        self.current_root_revision = details.get("current_root_revision")
        self.current_root_hash = str(details.get("current_root_hash") or "")
        self.current_root_ciphertext_b64 = str(
            details.get("current_root_ciphertext") or ""
        )
        self.current_shard_revision = details.get("current_shard_revision")
        self.current_shard_hash = str(details.get("current_shard_hash") or "")
        self.current_shard_ciphertext_b64 = str(
            details.get("current_shard_ciphertext") or ""
        )
        self.remote_folder_id = str(details.get("remote_folder_id") or "")

    def current_manifest_ciphertext_bytes(self) -> bytes:
        """Decoded server-head manifest envelope (legacy shape only).

        Empty bytes when the conflict was a sharded one. Callers in the
        sharded path use :meth:`current_shard_ciphertext_bytes` /
        :meth:`current_root_ciphertext_bytes`.
        """
        import base64

        if not self.current_manifest_ciphertext_b64:
            return b""
        return base64.b64decode(self.current_manifest_ciphertext_b64)

    def current_shard_ciphertext_bytes(self) -> bytes:
        """Decoded server-head shard envelope, ready for
        :meth:`Vault.decrypt_shard_envelope`. Empty bytes when the
        server didn't include a shard payload (root-only conflict).
        """
        import base64

        if not self.current_shard_ciphertext_b64:
            return b""
        return base64.b64decode(self.current_shard_ciphertext_b64)

    def current_root_ciphertext_bytes(self) -> bytes:
        """Decoded server-head root envelope, ready for
        :meth:`Vault.decrypt_root_envelope`. Empty bytes when the
        server didn't include a root payload (shard-only conflict).
        """
        import base64

        if not self.current_root_ciphertext_b64:
            return b""
        return base64.b64decode(self.current_root_ciphertext_b64)


class VaultIdentityMismatchError(RuntimeError):
    """Relay row at this vault_id is encrypted under a different master key.

    Raised by the resume worker when ``complete_pending_publish`` probes
    the relay and the existing header envelope either won't decrypt
    under our master key (AEAD tag mismatch — bytes belong to a stranger)
    or decrypts but reports a different ``genesis_fingerprint``
    (defense-in-depth against future header changes).

    Review §4.H2: pre-fix the resume worker treated "relay has a row at
    this vault_id" as proof of ownership and PUT a fresh recovery
    envelope on top — even if the row was created under a foreign master
    key. The UI saw "Resume succeeded" while the relay row was double-
    corrupted: the stranger's data stayed put, our recovery envelope
    overwrote one of theirs, and our local config believed it owned the
    row. Cross-relay configuration is the trigger (user repoints
    ``server_url`` to a new relay where ``vault_id`` happens to collide).
    """

    def __init__(self, *, vault_id: str) -> None:
        self.vault_id = str(vault_id)
        super().__init__(
            f"vault_identity_mismatch: relay row {self.vault_id!r} is not "
            "encrypted under this device's master key. Discard the pending "
            "publish and start a fresh setup, or repoint the config back "
            "to the original relay."
        )
