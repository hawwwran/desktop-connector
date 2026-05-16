"""SO-3: lightweight per-file persistence for batched-mode uploads.

Unlike the per-chunk :class:`UploadSession` used by ``upload_file``, a
batched-mode stub records only what the next cycle's prep needs to
make chunk_ids stable across a kill: the ``entry_id`` /
``version_id`` assigned the first time we encrypted this
``(vault, remote_path, content_fingerprint)`` tuple. When the batch
publish succeeds, the stub is cleared. When the batch dies (kill,
network, CAS exhaustion), the stub survives so the retry re-uses the
same ids and the relay HEAD-and-skips the chunks that already landed.

Stubs live in ``<cache_dir>/batched/<session_id>.json`` so they don't
pollute the resume banner — ``list_resumable_sessions`` only scans the
parent directory and never sees the batched subdirectory. The
duplication of ``session_id`` namespaces is fine: the directory split
is the namespace.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..atomic import atomic_write_file


_BATCH_SUBDIR = "batched"


@dataclass(frozen=True)
class BatchedUploadStub:
    """Per-file dedupe pin for SO-3 batched mode."""

    session_id: str
    vault_id: str
    remote_path: str
    entry_id: str
    version_id: str
    content_fingerprint: str
    created_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "vault_id": self.vault_id,
            "remote_path": self.remote_path,
            "entry_id": self.entry_id,
            "version_id": self.version_id,
            "content_fingerprint": self.content_fingerprint,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BatchedUploadStub":
        return cls(
            session_id=str(data["session_id"]),
            vault_id=str(data["vault_id"]),
            remote_path=str(data["remote_path"]),
            entry_id=str(data["entry_id"]),
            version_id=str(data["version_id"]),
            content_fingerprint=str(data["content_fingerprint"]),
            created_at=str(data.get("created_at", "")),
        )


def default_batch_cache_dir(parent: Path) -> Path:
    """Subdirectory under ``parent`` (a session cache dir) where the
    batched stubs live. Created if missing."""
    target = Path(parent) / _BATCH_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def save_stub(stub: BatchedUploadStub, cache_dir: Path) -> Path:
    """Atomically persist a stub at
    ``<cache_dir>/batched/<session_id>.json``."""
    batch_dir = default_batch_cache_dir(cache_dir)
    target = batch_dir / f"{stub.session_id}.json"
    payload = json.dumps(stub.to_json(), separators=(",", ":")).encode("utf-8")
    atomic_write_file(target, payload)
    return target


def clear_stub(session_id: str, cache_dir: Path) -> None:
    """Best-effort remove a stub by id. Missing file is fine."""
    batch_dir = default_batch_cache_dir(cache_dir)
    try:
        (batch_dir / f"{session_id}.json").unlink()
    except FileNotFoundError:
        return


def find_matching_stub(
    *,
    vault_id: str,
    remote_path: str,
    content_fingerprint: str,
    cache_dir: Path,
) -> BatchedUploadStub | None:
    """Return the stub whose tuple ``(vault, path, fingerprint)``
    matches, or ``None`` if there's nothing on disk for this file in
    its current shape.

    Stale stubs (different content for the same path) are intentionally
    skipped here, not deleted — :func:`reap_stubs_for_path` is the
    explicit cleanup path so callers can decide when to drop them.
    """
    batch_dir = default_batch_cache_dir(cache_dir)
    for path in sorted(batch_dir.glob("*.json")):
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            stub = BatchedUploadStub.from_json(data)
        except (KeyError, TypeError, ValueError):
            continue
        if stub.vault_id != vault_id:
            continue
        if stub.remote_path != remote_path:
            continue
        if stub.content_fingerprint != content_fingerprint:
            continue
        return stub
    return None


def reap_stubs_for_path(
    *,
    vault_id: str,
    remote_path: str,
    cache_dir: Path,
    keep_fingerprint: str | None = None,
) -> int:
    """Drop every stub for ``(vault_id, remote_path)`` whose
    ``content_fingerprint`` differs from ``keep_fingerprint``.

    Used when the file's bytes changed between the first prep attempt
    and a retry — the prior stub's version_id no longer corresponds to
    the current content, so we clear it (and let the retry write a
    fresh stub). Returns the count of stubs removed.
    """
    batch_dir = default_batch_cache_dir(cache_dir)
    removed = 0
    for path in sorted(batch_dir.glob("*.json")):
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            stub = BatchedUploadStub.from_json(data)
        except (KeyError, TypeError, ValueError):
            continue
        if stub.vault_id != vault_id:
            continue
        if stub.remote_path != remote_path:
            continue
        if keep_fingerprint is not None and stub.content_fingerprint == keep_fingerprint:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def make_stub(
    *,
    vault_id: str,
    remote_path: str,
    entry_id: str,
    version_id: str,
    content_fingerprint: str,
) -> BatchedUploadStub:
    """Build a stub with a fresh random session_id + an RFC 3339
    ``created_at`` timestamp."""
    return BatchedUploadStub(
        session_id=secrets.token_hex(8),
        vault_id=vault_id,
        remote_path=remote_path,
        entry_id=entry_id,
        version_id=version_id,
        content_fingerprint=content_fingerprint,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )


__all__ = [
    "BatchedUploadStub",
    "clear_stub",
    "default_batch_cache_dir",
    "find_matching_stub",
    "make_stub",
    "reap_stubs_for_path",
    "save_stub",
]
