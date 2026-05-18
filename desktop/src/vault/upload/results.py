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
    # Sharded folder-state at completion: ``root`` is the published root
    # envelope plaintext, ``shard`` is the published shard plaintext for
    # ``remote_folder_id``. Callers that need the legacy unified shape
    # synthesize via ``assemble_unified_manifest(root, {remote_folder_id:
    # shard})`` at the consumption point — the producer no longer carries
    # a unified copy through, so other folders' entries are never stale.
    root: dict[str, Any]
    shard: dict[str, Any]
    remote_folder_id: str
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
    # Sharded folder-state at completion (see FolderUploadResult docstring).
    root: dict[str, Any]
    shard: dict[str, Any]
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


@dataclass(frozen=True)
class PreparedUpload:
    """SO-3: per-file output of the chunk-PUT phase, ready to be folded
    into a batched manifest publish.

    ``prepare_upload_for_batch`` encrypts + PUTs the file's chunks but
    *does not* publish a manifest revision. The caller adds
    ``version_payload`` to a per-binding batch and runs one CAS publish
    for the whole batch (the SO-3 amortization). Re-running prep after a
    kill is safe — a persisted :class:`BatchedUploadStub` keyed by
    ``(vault, path, fingerprint)`` pins the same ``version_id`` across
    runs so the chunk_ids match and the relay HEAD-and-skips chunks
    that already landed. ``stub_session_id`` is the stub's identifier
    so the cycle's flush can clear it after a successful publish.

    ``skipped_identical`` short-circuits the caller: the file's bytes
    already match the latest live version on the remote, so nothing
    needs to enter the batch. The caller still owes a local-entry
    upsert + pending-op delete to record the "already in sync" state.
    """

    entry_id: str
    version_id: str
    normalized_remote_path: str
    content_fingerprint: str
    logical_size: int
    chunks_uploaded: int
    chunks_skipped: int
    bytes_uploaded: int
    # Populated when ``skipped_identical`` is False — the per-file
    # manifest version payload the caller threads into the batch.
    version_payload: dict[str, Any] | None = None
    skipped_identical: bool = False
    # The dedupe-stub id the cycle clears after a successful publish.
    # None for skipped_identical (no stub was written).
    stub_session_id: str | None = None
    # The directory holding the batched-stub subdirectory so the flush
    # can locate the stub without re-deriving the default path.
    stub_cache_dir: str | None = None
