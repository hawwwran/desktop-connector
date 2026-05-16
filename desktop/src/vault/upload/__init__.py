"""Public surface of the vault upload paths.

Composed from topical submodules under this package; legacy
``from .vault.upload import …`` imports keep working unchanged
because Python resolves ``vault.upload`` as this package and finds
the names below in the package namespace.

Logger naming is preserved on purpose: each submodule does
``log = logging.getLogger(__name__)`` so the loggers are
``src.vault.upload.<submodule>`` — descendants of ``src.vault.upload``,
which means tests using ``assertLogs("src.vault.upload", …)`` keep
capturing every emit through Python's logging propagation.
"""

from ..relay_errors import VaultRelayError
from .batch_session import (
    BatchedUploadStub,
    clear_stub,
    find_matching_stub,
    make_stub,
    reap_expired_stubs,
    reap_stubs_for_path,
    save_stub,
)
from .constants import (
    CAS_MAX_RETRIES,
    CHUNK_SIZE,
    MAX_FILE_BYTES_DEFAULT,
    UploadMode,
)
from .conflict import detect_path_conflict, make_conflict_renamed_path
from .errors import (
    UploadConflictError,
    UploadFileTooLargeError,
    UploadSpecialFileSkipped,
    describe_quota_exceeded,
)
from .folder import upload_folder
from .ignore_patterns import _UNSUPPORTED_PATTERN_WARNED, _matches_ignore
from .protocols import UploadRelay, UploadVault
from .results import (
    FileSkipped,
    FolderUploadProgress,
    FolderUploadResult,
    PreparedUpload,
    UploadProgress,
    UploadResult,
)
from .resume import resume_upload
from .session import (
    UploadSession,
    clear_session,
    default_upload_resume_dir,
    list_resumable_sessions,
    save_session,
)
from .single_file import prepare_upload_for_batch, upload_file

__all__ = [
    "CAS_MAX_RETRIES",
    "CHUNK_SIZE",
    "MAX_FILE_BYTES_DEFAULT",
    "BatchedUploadStub",
    "FileSkipped",
    "FolderUploadProgress",
    "FolderUploadResult",
    "PreparedUpload",
    "UploadConflictError",
    "UploadFileTooLargeError",
    "UploadMode",
    "UploadProgress",
    "UploadRelay",
    "UploadResult",
    "UploadSession",
    "UploadSpecialFileSkipped",
    "UploadVault",
    "VaultRelayError",
    "clear_session",
    "clear_stub",
    "default_upload_resume_dir",
    "describe_quota_exceeded",
    "detect_path_conflict",
    "find_matching_stub",
    "list_resumable_sessions",
    "make_conflict_renamed_path",
    "make_stub",
    "reap_expired_stubs",
    "reap_stubs_for_path",
    "prepare_upload_for_batch",
    "resume_upload",
    "save_session",
    "save_stub",
    "upload_file",
    "upload_folder",
]
