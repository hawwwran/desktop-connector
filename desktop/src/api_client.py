"""Back-compat shim for the relay HTTP client.

The implementation lives under ``desktop/src/api/`` (composed from
topical mixins). This module re-exports the public surface so legacy
``from .api_client import …`` imports keep working unchanged.

External callers should prefer ``from .api import …`` going forward;
this shim will be removed once all in-tree imports have migrated.

Note: ``time``, ``requests``, and ``KeyManager`` are re-imported here
so existing tests that ``patch.object(api_client_mod.time, "sleep")``
or ``patch.object(api_client_mod.KeyManager, "encrypt_chunk")``
continue to take effect — both targets are singletons (the stdlib
``time`` module and the ``KeyManager`` class), so patches via any
reference propagate to the actual call sites in ``api/transfers_*``.
"""

import time  # noqa: F401  — re-exported for test patches

import requests  # noqa: F401  — re-exported for test patches

from .api import (
    ApiClient,
    CAPABILITY_CACHE_TTL_S,
    CAPABILITY_STREAM_V1,
    CHUNK_MAX_FAILURE_WINDOW_S,
    CHUNK_RETRY_DELAY_S,
    ChunkDownloadOutcome,
    ChunkUploadOutcome,
    DOWNLOAD_ABORTED,
    DOWNLOAD_AUTH_ERROR,
    DOWNLOAD_FAILED,
    DOWNLOAD_NETWORK_ERROR,
    DOWNLOAD_NOT_FOUND,
    DOWNLOAD_OK,
    DOWNLOAD_TOO_EARLY,
    DeviceRegistrationResult,
    STORAGE_FULL_MAX_WINDOW_S,
    STREAM_QUOTA_BACKOFF_RAMP_S,
    UPLOAD_ABORTED,
    UPLOAD_AUTH_ERROR,
    UPLOAD_FAILED,
    UPLOAD_NETWORK_ERROR,
    UPLOAD_NOT_FOUND,
    UPLOAD_OK,
    UPLOAD_STORAGE_FULL,
    _extract_abort_reason,
    _parse_retry_after_ms,
)
from .crypto import KeyManager  # noqa: F401  — re-exported for test patches

__all__ = [
    "ApiClient",
    "CAPABILITY_CACHE_TTL_S",
    "CAPABILITY_STREAM_V1",
    "CHUNK_MAX_FAILURE_WINDOW_S",
    "CHUNK_RETRY_DELAY_S",
    "ChunkDownloadOutcome",
    "ChunkUploadOutcome",
    "DOWNLOAD_ABORTED",
    "DOWNLOAD_AUTH_ERROR",
    "DOWNLOAD_FAILED",
    "DOWNLOAD_NETWORK_ERROR",
    "DOWNLOAD_NOT_FOUND",
    "DOWNLOAD_OK",
    "DOWNLOAD_TOO_EARLY",
    "DeviceRegistrationResult",
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
