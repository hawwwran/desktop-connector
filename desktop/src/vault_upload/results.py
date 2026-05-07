"""Dataclasses returned from / passed through the upload paths."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class FileSkipped:
    """A file the folder walker decided not to upload.

    The ``error`` reason (F-D13) is reserved for ``lstat`` failures —
    permission denied, dangling symlinks, transient I/O errors — so the
    user can tell those apart from a deliberately-skipped symlink /
    FIFO / socket / device. Pure stat-based classification of a
    successfully-stat'd file remains ``special``.
    """
    relative_path: str
    reason: Literal["ignored", "too_large", "special", "error"]
    size_bytes: int = 0
    errno: int = 0


@dataclass(frozen=True)
class FolderUploadResult:
    manifest: dict[str, Any]
    uploaded: list["UploadResult"]
    skipped: list[FileSkipped] = field(default_factory=list)

    @property
    def chunks_uploaded(self) -> int:
        return sum(r.chunks_uploaded for r in self.uploaded)

    @property
    def chunks_skipped(self) -> int:
        return sum(r.chunks_skipped for r in self.uploaded)

    @property
    def bytes_uploaded(self) -> int:
        return sum(r.bytes_uploaded for r in self.uploaded)


@dataclass(frozen=True)
class FolderUploadProgress:
    phase: Literal["walking", "uploading", "publishing", "done"]
    files_total: int
    files_completed: int
    bytes_total: int
    bytes_completed: int
    current_path: str = ""


@dataclass(frozen=True)
class UploadProgress:
    phase: str
    completed_chunks: int
    total_chunks: int
    bytes_uploaded: int = 0


@dataclass(frozen=True)
class UploadResult:
    manifest: dict[str, Any]
    entry_id: str
    version_id: str
    path: str
    remote_folder_id: str
    chunks_uploaded: int
    chunks_skipped: int
    bytes_uploaded: int
    logical_size: int
    content_fingerprint: str
    skipped_identical: bool = False
