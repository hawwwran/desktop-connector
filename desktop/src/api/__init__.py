"""Public API surface for the relay client.

Composed from topical mixins under ``api/``; ``api_client.py`` is a
back-compat shim that re-exports everything below for callers that
still import from the legacy module path.
"""

from .client import ApiClient
from .constants import (
    CAPABILITY_CACHE_TTL_S,
    CAPABILITY_STREAM_V1,
    CHUNK_MAX_FAILURE_WINDOW_S,
    CHUNK_RETRY_DELAY_S,
    DOWNLOAD_ABORTED,
    DOWNLOAD_AUTH_ERROR,
    DOWNLOAD_FAILED,
    DOWNLOAD_NETWORK_ERROR,
    DOWNLOAD_NOT_FOUND,
    DOWNLOAD_OK,
    DOWNLOAD_TOO_EARLY,
    STORAGE_FULL_MAX_WINDOW_S,
    STREAM_QUOTA_BACKOFF_RAMP_S,
    UPLOAD_ABORTED,
    UPLOAD_AUTH_ERROR,
    UPLOAD_FAILED,
    UPLOAD_NETWORK_ERROR,
    UPLOAD_NOT_FOUND,
    UPLOAD_OK,
    UPLOAD_STORAGE_FULL,
)
from .outcomes import (
    ChunkDownloadOutcome,
    ChunkUploadOutcome,
    DeviceRegistrationResult,
)
from .parsing import _extract_abort_reason, _parse_retry_after_ms

__all__ = [
    "ApiClient",
    "ChunkDownloadOutcome",
    "ChunkUploadOutcome",
    "DeviceRegistrationResult",
    "CAPABILITY_CACHE_TTL_S",
    "CAPABILITY_STREAM_V1",
    "CHUNK_MAX_FAILURE_WINDOW_S",
    "CHUNK_RETRY_DELAY_S",
    "DOWNLOAD_ABORTED",
    "DOWNLOAD_AUTH_ERROR",
    "DOWNLOAD_FAILED",
    "DOWNLOAD_NETWORK_ERROR",
    "DOWNLOAD_NOT_FOUND",
    "DOWNLOAD_OK",
    "DOWNLOAD_TOO_EARLY",
    "STORAGE_FULL_MAX_WINDOW_S",
    "STREAM_QUOTA_BACKOFF_RAMP_S",
    "UPLOAD_ABORTED",
    "UPLOAD_AUTH_ERROR",
    "UPLOAD_FAILED",
    "UPLOAD_NETWORK_ERROR",
    "UPLOAD_NOT_FOUND",
    "UPLOAD_OK",
    "UPLOAD_STORAGE_FULL",
    "_extract_abort_reason",
    "_parse_retry_after_ms",
]
