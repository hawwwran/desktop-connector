"""Persisted upload session — the "I/O log" that lets a killed
``upload_file`` resume across processes (T6.5).

Lives at ``<cache_dir>/<session_id>.json`` and is rewritten atomically
after every chunk PUT success.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..vault.atomic import atomic_write_file


@dataclass
class UploadSession:
    """Persisted across-process upload plan (T6.5).

    Lives at ``<cache_dir>/<session_id>.json`` and is rewritten in place
    after every chunk PUT success. Holds enough information to resume a
    killed mid-upload without re-deriving anything from the manifest.
    """
    session_id: str
    vault_id: str
    remote_folder_id: str
    remote_path: str
    entry_id: str
    version_id: str
    author_device_id: str
    content_fingerprint: str
    logical_size: int
    local_path: str
    chunk_size: int
    created_at: str
    chunks: list[dict[str, Any]]
    phase: Literal["uploading", "ready_to_publish", "complete"] = "uploading"

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "vault_id": self.vault_id,
            "remote_folder_id": self.remote_folder_id,
            "remote_path": self.remote_path,
            "entry_id": self.entry_id,
            "version_id": self.version_id,
            "author_device_id": self.author_device_id,
            "content_fingerprint": self.content_fingerprint,
            "logical_size": self.logical_size,
            "local_path": self.local_path,
            "chunk_size": self.chunk_size,
            "created_at": self.created_at,
            "chunks": list(self.chunks),
            "phase": self.phase,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "UploadSession":
        return cls(
            session_id=str(data["session_id"]),
            vault_id=str(data["vault_id"]),
            remote_folder_id=str(data["remote_folder_id"]),
            remote_path=str(data["remote_path"]),
            entry_id=str(data["entry_id"]),
            version_id=str(data["version_id"]),
            author_device_id=str(data["author_device_id"]),
            content_fingerprint=str(data.get("content_fingerprint", "")),
            logical_size=int(data["logical_size"]),
            local_path=str(data["local_path"]),
            chunk_size=int(data["chunk_size"]),
            created_at=str(data["created_at"]),
            chunks=list(data.get("chunks", [])),
            phase=data.get("phase", "uploading"),
        )


def default_upload_resume_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "desktop-connector" / "vault" / "uploads"


def save_session(session: UploadSession, cache_dir: Path) -> Path:
    """Atomically write the session JSON to ``<cache_dir>/<session_id>.json``."""
    cache_dir = Path(cache_dir)
    target = cache_dir / f"{session.session_id}.json"
    payload = json.dumps(session.to_json(), separators=(",", ":")).encode("utf-8")
    atomic_write_file(target, payload)
    return target


def clear_session(session_id: str, cache_dir: Path) -> None:
    cache_dir = Path(cache_dir)
    target = cache_dir / f"{session_id}.json"
    try:
        target.unlink()
    except FileNotFoundError:
        return


def list_resumable_sessions(vault_id: str, cache_dir: Path) -> list[UploadSession]:
    """Return every saved session that targets ``vault_id`` and is unfinished."""
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return []
    out: list[UploadSession] = []
    for path in sorted(cache_dir.glob("*.json")):
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            session = UploadSession.from_json(data)
        except (KeyError, TypeError, ValueError):
            continue
        if session.vault_id != vault_id:
            continue
        if session.phase == "complete":
            continue
        out.append(session)
    return out
