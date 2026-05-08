"""Public surface of the vault download paths.

Composed from topical submodules under this package; legacy
``from .vault_download import …`` imports keep working unchanged
because Python resolves ``vault_download`` as this package and finds
the names below in the package namespace.

Logger naming is preserved on purpose: each submodule does
``log = logging.getLogger(__name__)`` so the loggers are
``src.vault_download.<submodule>`` — descendants of ``src.vault_download``,
which means tests using ``assertLogs("src.vault_download", …)`` keep
capturing every emit through Python's logging propagation.

Two private helpers leak through this surface for genuine load-bearing
back-compat (don't add more without auditing the caller list):

- ``_load_cached_chunk`` — ``tests/protocol/test_desktop_vault_download``
  unit-tests it directly.
- ``_decrypt_chunk`` — ``windows_vault/tab_maintenance.py`` re-decrypts
  every chunk during the integrity check.

The ``_chunk_missing_sleep`` test seam is **not** re-exported here:
tests that need to skip real sleeps must
``mock.patch("src.vault_download.chunks._chunk_missing_sleep", …)``
because module-local lookups in :mod:`.chunks` won't see a re-binding
on this package object.
"""

from ..vault_crypto import normalize_vault_id
from ..vault_relay_errors import VaultChunkMissingError
from .cache import (
    DEFAULT_VAULT_CHUNK_CACHE_MAX_BYTES,
    _load_cached_chunk,
    default_vault_download_cache_dir,
    prune_vault_chunk_cache,
    vault_chunk_cache_path,
)
from .chunks import _decrypt_chunk
from .folder import download_folder
from .manifest import previous_version_filename
from .paths import (
    atomic_write_chunks,
    atomic_write_file,
    resolve_download_destination,
    resolve_folder_destination,
)
from .single_file import download_latest_file, download_version
from .types import (
    ChunkRelay,
    DownloadCancelled,
    DownloadProgress,
    DownloadVault,
    ExistingDestinationError,
    ExistingFilePolicy,
    VaultLocalDiskFullError,
)


__all__ = [
    "ChunkRelay",
    "DEFAULT_VAULT_CHUNK_CACHE_MAX_BYTES",
    "DownloadCancelled",
    "DownloadProgress",
    "DownloadVault",
    "ExistingDestinationError",
    "ExistingFilePolicy",
    "VaultChunkMissingError",
    "VaultLocalDiskFullError",
    "atomic_write_chunks",
    "atomic_write_file",
    "default_vault_download_cache_dir",
    "download_folder",
    "download_latest_file",
    "download_version",
    "normalize_vault_id",
    "previous_version_filename",
    "prune_vault_chunk_cache",
    "resolve_download_destination",
    "resolve_folder_destination",
    "vault_chunk_cache_path",
]
