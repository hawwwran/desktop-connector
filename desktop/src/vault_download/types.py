"""Shared types for the vault download paths.

Errors, runtime Protocols, and lightweight dataclasses used across the
download submodules. Kept together because each piece is too small to
justify a dedicated file and they compose into a single conceptual
"public-facing surface" for callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol


ExistingFilePolicy = Literal["fail", "overwrite", "keep_both", "cancel"]


class DownloadCancelled(Exception):
    """Raised when the caller chooses not to overwrite an existing file."""


class ExistingDestinationError(FileExistsError):
    """Raised when the destination exists and no overwrite policy was chosen."""


class VaultLocalDiskFullError(OSError):
    """Local destination volume does not have enough free space."""


class ChunkRelay(Protocol):
    def batch_head_chunks(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]: ...

    def get_chunk(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_id: str,
    ) -> bytes: ...


class DownloadVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...


@dataclass(frozen=True)
class DownloadProgress:
    phase: str
    completed_chunks: int
    total_chunks: int
    bytes_written: int = 0


@dataclass(frozen=True)
class _FolderFilePlan:
    display_path: str
    relative_path: Path
    remote_folder_id: str
    file_id: str
    entry: dict[str, Any]
    version: dict[str, Any]
    chunks: list[dict[str, Any]]
