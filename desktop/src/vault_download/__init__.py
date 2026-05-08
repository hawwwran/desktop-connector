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

Note on the ``_chunk_missing_sleep`` test seam: tests must
``mock.patch("src.vault_download.chunks._chunk_missing_sleep", …)``
because module-local lookups in :mod:`.chunks` won't see a re-binding
of the name on this package object. The re-export below is for
*reads* (callers checking the default), not patches.
"""

from ..vault_crypto import normalize_vault_id
from ..vault_relay_errors import VaultChunkMissingError
from .cache import (
    DEFAULT_VAULT_CHUNK_CACHE_MAX_BYTES,
    _load_cached_chunk,
    _store_cached_chunk,
    default_vault_download_cache_dir,
    prune_vault_chunk_cache,
    vault_chunk_cache_path,
)
from .chunks import (
    _CHUNK_MISSING_BASE_BACKOFF_S,
    _CHUNK_MISSING_CAP_BACKOFF_S,
    _CHUNK_MISSING_MAX_RETRIES,
    _chunk_missing_sleep,
    _decrypt_chunk,
    _ensure_all_chunks_present,
    _get_chunk_with_retry,
    _missing_retry_delay_s,
)
from .folder import download_folder
from .manifest import (
    _find_version,
    _folder_file_plans,
    _folder_for_display_path,
    _int_value,
    _latest_version,
    _safe_manifest_path_parts,
    _split_display_path,
    _unique_chunk_ids,
    _version_chunks,
    _version_tag,
    previous_version_filename,
)
from .paths import (
    _fsync_dir,
    _keep_both_folder_path,
    _keep_both_path,
    _nearest_existing_parent,
    _preflight_disk_space,
    _preflight_folder_disk_space,
    atomic_write_chunks,
    atomic_write_file,
    resolve_download_destination,
    resolve_folder_destination,
)
from .single_file import (
    _report,
    download_latest_file,
    download_version,
)
from .types import (
    ChunkRelay,
    DownloadCancelled,
    DownloadProgress,
    DownloadVault,
    ExistingDestinationError,
    ExistingFilePolicy,
    VaultLocalDiskFullError,
    _FolderFilePlan,
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
