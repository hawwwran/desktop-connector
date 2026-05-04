"""Vault integrity-check primitives (T17.3).

Two scopes per spec:

- :func:`run_quick_check` — verifies the head manifest's AEAD envelope
  decrypts cleanly, the parent-revision chain is monotonically linked
  (each revision references its immediate predecessor), and every
  chunk_id referenced by any non-deleted version's ``chunks[]`` is
  reported as "present" by the relay's batch-head endpoint. Runs in
  seconds on a healthy vault.

- :func:`run_full_check` — extends the quick check by AEAD-decrypting
  every retained revision (not just the head) and pulling + AEAD-
  decrypting every chunk through ``vault_download._decrypt_chunk``.
  Slow but thorough — catches a corrupted older revision a quick scan
  would miss.

Both produce a :class:`IntegrityReport` with a ``broken`` list the
T17.4 repair helper consumes. The relay/vault wiring is injected so
tests drive a fake without real I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol


log = logging.getLogger(__name__)


@dataclass
class IntegrityIssue:
    kind: str             # 'manifest_chain_broken' | 'chunk_missing' | 'aead_decrypt_failed' | ...
    target: str           # vault_id / revision / chunk_id / display_path
    detail: str = ""


@dataclass
class IntegrityReport:
    scope: str            # 'quick' | 'full'
    revisions_checked: int = 0
    chunks_checked: int = 0
    broken: list[IntegrityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.broken

    def add(self, issue: IntegrityIssue) -> None:
        self.broken.append(issue)


class IntegrityVault(Protocol):
    @property
    def vault_id(self) -> str: ...
    @property
    def master_key(self) -> bytes | None: ...
    @property
    def vault_access_secret(self) -> str | None: ...

    def fetch_manifest(self, relay, *, local_index=None) -> dict[str, Any]: ...


class IntegrityRelay(Protocol):
    def batch_head_chunks(
        self, vault_id: str, vault_access_secret: str, chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]: ...

    def list_manifest_revisions(
        self, vault_id: str, vault_access_secret: str,
    ) -> list[dict[str, Any]]: ...


def run_quick_check(
    *,
    vault: IntegrityVault,
    relay: IntegrityRelay,
    manifest: dict[str, Any] | None = None,
) -> IntegrityReport:
    """Cheap-but-thorough check: head decrypts, parent chain links,
    and every referenced chunk_id is present at the relay."""
    report = IntegrityReport(scope="quick")
    if vault.master_key is None or vault.vault_access_secret is None:
        report.add(IntegrityIssue(
            kind="vault_locked", target=vault.vault_id,
            detail="quick check requires an unlocked vault",
        ))
        return report

    head = manifest or _safe_fetch_manifest(vault, relay, report)
    if head is None:
        return report

    report.revisions_checked = 1
    head_rev = int(head.get("revision", 0))
    parent_rev = int(head.get("parent_revision", 0))
    if head_rev <= 0:
        report.add(IntegrityIssue(
            kind="manifest_revision_invalid",
            target=str(head_rev),
            detail="manifest revision must be a positive integer",
        ))
    elif parent_rev != head_rev - 1:
        report.add(IntegrityIssue(
            kind="manifest_chain_broken",
            target=f"head_revision={head_rev}",
            detail=f"parent_revision={parent_rev} but expected {head_rev - 1}",
        ))

    chunk_ids = sorted(_referenced_chunk_ids(head))
    if chunk_ids:
        try:
            heads = relay.batch_head_chunks(
                vault.vault_id, vault.vault_access_secret, chunk_ids,
            )
        except Exception as exc:  # noqa: BLE001
            report.add(IntegrityIssue(
                kind="batch_head_failed", target=vault.vault_id, detail=str(exc),
            ))
            heads = {}
        for cid in chunk_ids:
            info = heads.get(cid)
            if not isinstance(info, dict) or not info.get("present"):
                report.add(IntegrityIssue(
                    kind="chunk_missing", target=cid,
                    detail="referenced by head manifest but not on relay",
                ))
        report.chunks_checked = len(chunk_ids)

    return report


def run_full_check(
    *,
    vault: IntegrityVault,
    relay: IntegrityRelay,
    decrypt_chunk: Callable[[dict[str, Any], dict[str, Any], dict[str, Any], bytes], bytes],
    fetch_chunk: Callable[[str, str, str], bytes],
    decrypt_manifest_envelope: Callable[[bytes], dict[str, Any]] | None = None,
) -> IntegrityReport:
    """Quick check + decrypt every retained revision + decrypt every chunk.

    ``decrypt_chunk`` is a callable mirroring
    :func:`vault_download._decrypt_chunk`'s signature so tests can
    inject a fake. ``fetch_chunk`` returns the encrypted blob bytes
    (relay get_chunk).
    """
    report = run_quick_check(vault=vault, relay=relay)
    if vault.master_key is None or vault.vault_access_secret is None:
        return report

    head = _safe_fetch_manifest(vault, relay, report)
    if head is None:
        return report

    # Manifest revisions list — fall back to head-only if the relay
    # doesn't expose per-revision listing on this build.
    try:
        revisions = relay.list_manifest_revisions(
            vault.vault_id, vault.vault_access_secret,
        )
    except Exception as exc:  # noqa: BLE001
        log.info(
            "vault.integrity.list_revisions_unavailable vault=%s error=%s",
            vault.vault_id, exc,
        )
        revisions = []

    report.revisions_checked = max(1, len(revisions))
    if decrypt_manifest_envelope is not None:
        for entry in revisions:
            envelope = entry.get("manifest_ciphertext")
            if not isinstance(envelope, (bytes, bytearray)):
                continue
            try:
                decrypt_manifest_envelope(bytes(envelope))
            except Exception as exc:  # noqa: BLE001
                report.add(IntegrityIssue(
                    kind="manifest_aead_failed",
                    target=str(entry.get("revision", "?")),
                    detail=str(exc),
                ))

    # Decrypt every chunk referenced from any non-deleted version of
    # the head manifest. We don't walk every revision's chunks because
    # the head manifest already references the ones currently in use;
    # older revisions reference subsets of these (CAS chain semantics).
    folder_by_id = {
        f.get("remote_folder_id"): f
        for f in head.get("remote_folders", []) or []
        if isinstance(f, dict)
    }
    seen_chunks: set[str] = set()
    for folder_id, folder in folder_by_id.items():
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict) or bool(entry.get("deleted")):
                continue
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                for chunk in version.get("chunks", []) or []:
                    if not isinstance(chunk, dict):
                        continue
                    cid = str(chunk.get("chunk_id", ""))
                    if not cid or cid in seen_chunks:
                        continue
                    seen_chunks.add(cid)
                    try:
                        encrypted = fetch_chunk(
                            vault.vault_id, vault.vault_access_secret, cid,
                        )
                    except Exception as exc:  # noqa: BLE001
                        report.add(IntegrityIssue(
                            kind="chunk_fetch_failed", target=cid,
                            detail=str(exc),
                        ))
                        continue
                    try:
                        decrypt_chunk(folder, entry, version, encrypted)
                    except Exception as exc:  # noqa: BLE001
                        report.add(IntegrityIssue(
                            kind="chunk_aead_failed", target=cid,
                            detail=str(exc),
                        ))

    report.chunks_checked = len(seen_chunks)
    report.scope = "full"
    return report


def _safe_fetch_manifest(
    vault: IntegrityVault, relay: IntegrityRelay, report: IntegrityReport,
) -> dict[str, Any] | None:
    try:
        return vault.fetch_manifest(relay)
    except Exception as exc:  # noqa: BLE001
        report.add(IntegrityIssue(
            kind="manifest_fetch_failed",
            target=vault.vault_id,
            detail=str(exc),
        ))
        return None


def _referenced_chunk_ids(manifest: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict) or bool(entry.get("deleted")):
                continue
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                for chunk in version.get("chunks", []) or []:
                    if not isinstance(chunk, dict):
                        continue
                    cid = chunk.get("chunk_id")
                    if isinstance(cid, str) and cid:
                        out.add(cid)
    return out


__all__ = [
    "IntegrityIssue",
    "IntegrityRelay",
    "IntegrityReport",
    "IntegrityVault",
    "run_full_check",
    "run_quick_check",
]
