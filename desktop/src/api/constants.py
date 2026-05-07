"""Tuning knobs and status-string vocab for the API client.

Module-level so each topical mixin reads constants from this single
source of truth, and the legacy ``api_client.py`` shim can re-export
them unchanged.
"""

CHUNK_RETRY_DELAY_S = 5.0
CHUNK_MAX_FAILURE_WINDOW_S = 120.0

# Streaming sender mid-stream 507 backoff (see streaming-improvement.md
# §5.4 and desktop-streaming-relay-plan.md §C.4). 507s during streaming
# mean the recipient's quota is full — the sender sleeps, retries the
# SAME chunk until the recipient drains. Honours the standard 30-min
# STORAGE_FULL_MAX_WINDOW_S ceiling below; on expiry the transfer is
# aborted with reason=sender_failed.
STREAM_QUOTA_BACKOFF_RAMP_S = (2.0, 4.0, 8.0, 16.0, 30.0)
# Upper bound on how long a single transfer can sit in WAITING state
# (server replied 507 "storage full") before we give up and mark it
# failed. Without a cap, a closed send-files window would leave its
# row stuck "waiting" forever since there's no longer a subprocess to
# retry. 30 minutes is enough to outlast any reasonable chunk-drain
# on the recipient side while still cleaning up abandoned sends.
STORAGE_FULL_MAX_WINDOW_S = 30 * 60

# Capability advertised by a streaming-capable server in GET /api/health.
CAPABILITY_STREAM_V1 = "stream_v1"

# How long a capability probe result is cached. A server that flips
# streamingEnabled via config.json should propagate within a minute
# without clients hammering /api/health every chunk.
CAPABILITY_CACHE_TTL_S = 60.0


# --- Typed outcomes for chunk upload / download -----------------------
#
# Kept as plain string constants (matching the existing init_transfer
# convention) plus small dataclasses for payload. Streaming-capable
# callers branch on `status`; classic callers only need `UPLOAD_OK` vs
# everything-else.

UPLOAD_OK = "ok"
UPLOAD_STORAGE_FULL = "storage_full"    # 507 — mid-stream quota gate
UPLOAD_ABORTED = "aborted"              # 410 — recipient (or self) aborted
UPLOAD_NOT_FOUND = "not_found"          # 404 — transfer gone / unknown
UPLOAD_AUTH_ERROR = "auth_error"        # 401 / 403
UPLOAD_NETWORK_ERROR = "network_error"  # no response at all
UPLOAD_FAILED = "failed"                # 4xx / 5xx we don't specifically
                                        # distinguish (400, 422, 500…)


DOWNLOAD_OK = "ok"
DOWNLOAD_TOO_EARLY = "too_early"          # 425 — chunk not stored yet
DOWNLOAD_ABORTED = "aborted"              # 410
DOWNLOAD_NOT_FOUND = "not_found"          # 404
DOWNLOAD_AUTH_ERROR = "auth_error"        # 401 / 403
DOWNLOAD_NETWORK_ERROR = "network_error"  # no response
DOWNLOAD_FAILED = "failed"
