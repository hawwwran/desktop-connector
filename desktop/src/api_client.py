"""
API client for communicating with the PHP relay server.
"""

import base64
import logging
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

from .connection import ConnectionManager
from .crypto import KeyManager, CHUNK_SIZE


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


@dataclass
class ChunkUploadOutcome:
    status: str
    body: dict | None = None
    abort_reason: str | None = None
    http_status: int | None = None


DOWNLOAD_OK = "ok"
DOWNLOAD_TOO_EARLY = "too_early"          # 425 — chunk not stored yet
DOWNLOAD_ABORTED = "aborted"              # 410
DOWNLOAD_NOT_FOUND = "not_found"          # 404
DOWNLOAD_AUTH_ERROR = "auth_error"        # 401 / 403
DOWNLOAD_NETWORK_ERROR = "network_error"  # no response
DOWNLOAD_FAILED = "failed"


@dataclass
class ChunkDownloadOutcome:
    status: str
    data: bytes | None = None
    retry_after_ms: int | None = None
    abort_reason: str | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class DeviceRegistrationResult:
    status_code: int
    body: dict | None = None

    @property
    def is_successful(self) -> bool:
        return 200 <= self.status_code < 300 and self.body is not None


log = logging.getLogger(__name__)


def _parse_retry_after_ms(resp: "requests.Response") -> int:
    """Read the server-suggested retry delay from a 425 response.

    Preference order: body `retry_after_ms` (ms precision) → header
    `Retry-After` (seconds) → default 1000 ms. Server emits both; mobile
    / desktop tooling only reliably reads headers, so we accept either.
    """
    default_ms = 1000
    try:
        body = resp.json()
        if isinstance(body, dict):
            ms = body.get("retry_after_ms")
            if isinstance(ms, int) and ms > 0:
                return ms
    except (ValueError, AttributeError):
        pass
    header = resp.headers.get("Retry-After")
    if header:
        try:
            secs = int(header)
            if secs > 0:
                return secs * 1000
        except ValueError:
            pass
    return default_ms


def _extract_abort_reason(resp: "requests.Response") -> str | None:
    """Read `abort_reason` from a 410 body, if present."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            reason = body.get("abort_reason")
            if isinstance(reason, str) and reason:
                return reason
    except (ValueError, AttributeError):
        pass
    return None


class ApiClient:
    """High-level API client wrapping ConnectionManager for server operations."""

    def __init__(self, connection: ConnectionManager, crypto: KeyManager):
        self.conn = connection
        self.crypto = crypto

    def register(self, server_url: str, device_type: str = "desktop") -> dict | None:
        """Register this device with the server. Returns {device_id, auth_token} or None."""
        result = self.register_with_status(server_url, device_type)
        if result is not None and result.is_successful:
            return result.body
        return None

    def register_with_status(
        self,
        server_url: str,
        device_type: str = "desktop",
    ) -> DeviceRegistrationResult | None:
        """Register this device and expose the HTTP status for recovery decisions."""
        try:
            resp = requests.post(
                f"{server_url}/api/devices/register",
                json={
                    "public_key": self.crypto.get_public_key_b64(),
                    "device_type": device_type,
                },
                timeout=10,
            )
            body = None
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                pass
            if resp.status_code in (200, 201):
                return DeviceRegistrationResult(resp.status_code, body)
            log.error("Registration failed: %d %s", resp.status_code, resp.text)
            return DeviceRegistrationResult(resp.status_code, body)
        except requests.RequestException as e:
            log.error("Registration request failed: %s", e)
        return None

    def send_pairing_request(self, desktop_id: str, phone_pubkey: str) -> bool:
        """Send a pairing request (phone → desktop)."""
        resp = self.conn.request("POST", "/api/pairing/request", json={
            "desktop_id": desktop_id,
            "phone_pubkey": phone_pubkey,
        })
        return resp is not None and resp.status_code in (200, 201)

    def poll_pairing(self) -> list[dict]:
        """Poll for incoming pairing requests. Returns list of {id, phone_id, phone_pubkey}."""
        resp = self.conn.request("GET", "/api/pairing/poll")
        if resp and resp.status_code == 200:
            return resp.json().get("requests", [])
        return []

    def confirm_pairing(self, phone_id: str) -> bool:
        resp = self.conn.request("POST", "/api/pairing/confirm", json={"phone_id": phone_id})
        return resp is not None and resp.status_code == 200

    def init_transfer(self, transfer_id: str, recipient_id: str,
                      encrypted_meta: str, chunk_count: int,
                      *, mode: str = "classic") -> tuple[str, str | None]:
        """Initialize a transfer on the server.

        Returns (status, negotiated_mode):
          * ('ok', 'classic' | 'streaming') — 201, transfer registered.
             negotiated_mode reflects what the server actually chose; may
             differ from the requested `mode` (server downgrades to
             classic when the recipient is offline, streamingEnabled is
             off, etc. — see docs/plans/streaming-improvement.md §gap
             10–11).
          * ('storage_full', None) — 507, recipient's quota exceeded;
             caller should keep retrying and show WAITING. Classic path
             only — streaming init skips the projected reservation.
          * ('too_large', None) — 413, transfer alone exceeds the
             server's quota. Terminal.
          * ('failed', None) — anything else (network exception, 4xx,
             5xx) — caller decides retry budget.

        Old callers that pass no `mode` get classic behaviour; the
        returned negotiated_mode is always 'classic' on success.
        """
        payload = {
            "transfer_id": transfer_id,
            "recipient_id": recipient_id,
            "encrypted_meta": encrypted_meta,
            "chunk_count": chunk_count,
        }
        if mode != "classic":
            payload["mode"] = mode
        resp = self.conn.request("POST", "/api/transfers/init", json=payload)
        if resp is None:
            return "failed", None
        if resp.status_code == 201:
            negotiated = "classic"
            try:
                body = resp.json()
                if isinstance(body, dict):
                    nm = body.get("negotiated_mode")
                    if isinstance(nm, str) and nm in ("classic", "streaming"):
                        negotiated = nm
            except (ValueError, AttributeError):
                pass
            return "ok", negotiated
        if resp.status_code == 507:
            return "storage_full", None
        if resp.status_code == 413:
            # Transfer itself exceeds the server's quota — terminal, no
            # amount of waiting makes it fit. Caller bails immediately
            # instead of entering WAITING / retry loops.
            return "too_large", None
        return "failed", None

    def abort_transfer(self, transfer_id: str, reason: str | None = None) -> bool:
        """Either-party abort. Server wipes chunks + row and — for
        streaming transfers — notifies the other party via FCM so its
        long-poll / next chunk call returns 410.

        `reason` is one of:
          * 'sender_abort'     — caller is the sender, explicit cancel
          * 'sender_failed'    — caller is the sender, gave up after a
                                  retry budget (quota_timeout, network)
          * 'recipient_abort'  — caller is the recipient
          * None               — legacy sender-side cancel. Back-compat
                                  alias for 'sender_abort' but the
                                  server also accepts it without a body.

        Cross-role reasons (sender passing 'recipient_abort' or vice
        versa) are rejected server-side with 400. Returns True on any
        2xx. A 410 on a transfer the other side already aborted is
        still reported as False — the cleanup already happened and the
        caller doesn't need to retry.
        """
        body = {"reason": reason} if reason else None
        resp = self.conn.request(
            "DELETE",
            f"/api/transfers/{transfer_id}",
            json=body,
        )
        return resp is not None and 200 <= resp.status_code < 300

    def cancel_transfer(self, transfer_id: str) -> bool:
        """Back-compat alias for `abort_transfer(transfer_id, 'sender_abort')`.

        Preserved so older entry points (history window's cancel button,
        one-shot `--send` failure cleanup) keep working unchanged. New
        callers should use `abort_transfer` with an explicit reason so
        the opposite party sees the right UI label.
        """
        return self.abort_transfer(transfer_id, "sender_abort")

    def upload_chunk(self, transfer_id: str, chunk_index: int, data: bytes) -> ChunkUploadOutcome:
        """Upload an encrypted chunk.

        Returns a typed outcome so the streaming sender can distinguish
        a transient quota gate (507 → keep retrying with backoff) from
        a terminal abort (410 → recipient/self aborted, stop uploading)
        from a generic failure. Classic senders only care about
        ``status == UPLOAD_OK``.
        """
        resp = self.conn.request(
            "POST",
            f"/api/transfers/{transfer_id}/chunks/{chunk_index}",
            data=data,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Device-ID": self.conn.device_id,
                "Authorization": f"Bearer {self.conn.auth_token}",
            },
        )
        if resp is None:
            return ChunkUploadOutcome(status=UPLOAD_NETWORK_ERROR)
        code = resp.status_code
        if code == 200:
            try:
                body = resp.json()
            except (ValueError, AttributeError):
                body = None
            return ChunkUploadOutcome(
                status=UPLOAD_OK,
                body=body if isinstance(body, dict) else None,
                http_status=code,
            )
        if code == 507:
            return ChunkUploadOutcome(status=UPLOAD_STORAGE_FULL, http_status=code)
        if code == 410:
            return ChunkUploadOutcome(
                status=UPLOAD_ABORTED,
                abort_reason=_extract_abort_reason(resp),
                http_status=code,
            )
        if code == 404:
            return ChunkUploadOutcome(status=UPLOAD_NOT_FOUND, http_status=code)
        if code in (401, 403):
            return ChunkUploadOutcome(status=UPLOAD_AUTH_ERROR, http_status=code)
        return ChunkUploadOutcome(status=UPLOAD_FAILED, http_status=code)

    def get_pending_transfers(self) -> list[dict]:
        """Get list of pending transfers for this device.

        Guards resp.json() — under load a shared-hosting server can
        return 200 OK with an empty/partial body (PHP dies mid-response).
        Falling through to `[]` is the right fallback: we'll try again
        on the next poll tick without crashing the poll loop."""
        resp = self.conn.request("GET", "/api/transfers/pending")
        if resp and resp.status_code == 200:
            try:
                return resp.json().get("transfers", [])
            except ValueError:  # requests' JSONDecodeError subclasses ValueError
                log.warning("transfer.pending.malformed body_length=%d",
                            len(resp.content or b""))
                return []
        return []

    def download_chunk(self, transfer_id: str, chunk_index: int) -> ChunkDownloadOutcome:
        """Download an encrypted chunk.

        Returns a typed outcome so streaming recipients can tell the
        difference between "not uploaded yet, wait and retry" (425)
        and "transfer aborted, stop trying" (410). Classic recipients
        only look at ``status == DOWNLOAD_OK`` and ``data``.
        """
        resp = self.conn.request("GET", f"/api/transfers/{transfer_id}/chunks/{chunk_index}")
        if resp is None:
            return ChunkDownloadOutcome(status=DOWNLOAD_NETWORK_ERROR)
        code = resp.status_code
        if code == 200:
            return ChunkDownloadOutcome(
                status=DOWNLOAD_OK,
                data=resp.content,
                http_status=code,
            )
        if code == 425:
            return ChunkDownloadOutcome(
                status=DOWNLOAD_TOO_EARLY,
                retry_after_ms=_parse_retry_after_ms(resp),
                http_status=code,
            )
        if code == 410:
            return ChunkDownloadOutcome(
                status=DOWNLOAD_ABORTED,
                abort_reason=_extract_abort_reason(resp),
                http_status=code,
            )
        if code == 404:
            return ChunkDownloadOutcome(status=DOWNLOAD_NOT_FOUND, http_status=code)
        if code in (401, 403):
            return ChunkDownloadOutcome(status=DOWNLOAD_AUTH_ERROR, http_status=code)
        return ChunkDownloadOutcome(status=DOWNLOAD_FAILED, http_status=code)

    def ack_transfer(self, transfer_id: str) -> bool:
        """Acknowledge transfer receipt (classic). Server will delete blobs.

        Streaming transfers use per-chunk ``ack_chunk`` instead; calling
        this endpoint on a streaming transfer after the final per-chunk
        ACK is a no-op but is harmless. Calling it BEFORE per-chunk
        ACKs in streaming mode is rejected server-side.
        """
        resp = self.conn.request("POST", f"/api/transfers/{transfer_id}/ack")
        return resp is not None and resp.status_code == 200

    def ack_chunk(self, transfer_id: str, chunk_index: int) -> bool:
        """Per-chunk acknowledgement (streaming only).

        Signals the server that this chunk has been durably written on
        the recipient side so the blob can be deleted immediately — the
        core of the streaming-relay storage win. Idempotent: repeated
        ACKs on the same index return 200 without error.

        Classic transfers reject per-chunk ACK with 400; callers must
        only call this when ``negotiated_mode == 'streaming'``.
        """
        resp = self.conn.request(
            "POST",
            f"/api/transfers/{transfer_id}/chunks/{chunk_index}/ack",
        )
        return resp is not None and resp.status_code == 200

    def send_file(self, filepath: Path, recipient_id: str, symmetric_key: bytes,
                  filename_override: str | None = None,
                  on_progress: callable = None,
                  *,
                  on_stream_progress: callable = None,
                  streaming: bool = True) -> str | None:
        """
        Encrypt and upload a file to a recipient using a chunked pipeline:
        read one chunk → encrypt → upload → repeat. Memory is bounded by
        CHUNK_SIZE regardless of file size.

        Mode is negotiated with the server at init time:
          * ``streaming=True`` (default) + server advertises ``stream_v1``
            + filename is NOT a ``.fn.*`` command transfer → request
            ``mode=streaming``. Server honours it when the recipient is
            online; otherwise silently downgrades to classic.
          * ``streaming=False`` OR the above conditions fail → classic
            mode (store-then-forward).

        Callbacks:
          * ``on_progress(tid, uploaded, total)`` — fired on the classic
            upload path (unchanged from pre-streaming). Sentinel values
            -1 and -2 on init-waiting / init-too-large are still emitted.
          * ``on_stream_progress(tid, uploaded, total, state)`` — fired
            on the streaming upload path. ``state`` ∈ ``{'sending',
            'waiting_stream', 'aborted', 'failed'}``. Both callbacks may
            be None; the upload proceeds silently in that case.

        Per-chunk retry semantics:
          * Classic: 5s cadence, 120s budget per chunk; then abort.
          * Streaming: see ``_upload_stream`` — 507 enters waiting_stream
            with exponential backoff and the standard 30-min window,
            410 flips to aborted (recipient aborted), network errors
            follow the classic 2-min budget.

        Returns transfer_id on success, None on failure.
        """
        display = filename_override or filepath.name
        file_size = filepath.stat().st_size
        log.info("transfer.upload.started name=%s bytes=%d recipient=%s",
                 display, file_size, recipient_id[:12])

        chunk_count = max(1, math.ceil(file_size / CHUNK_SIZE))
        base_nonce = KeyManager.generate_base_nonce()
        encrypted_meta = KeyManager.build_encrypted_metadata(
            filename=display,
            mime_type=KeyManager.guess_mime(display),
            size=file_size,
            chunk_count=chunk_count,
            base_nonce=base_nonce,
            key=symmetric_key,
        )
        transfer_id = str(uuid.uuid4())

        # .fn.* command transfers always go classic — streaming adds
        # round-trip overhead to what's already a tiny single-chunk
        # payload. See streaming-improvement.md §9 non-goals.
        is_fn = display.startswith(".fn.")
        requested_mode = "classic"
        if streaming and not is_fn and self.supports_streaming():
            requested_mode = "streaming"

        # Fire the initial progress callback BEFORE init_transfer so the caller
        # can land a history row in "uploading 0/N" state. If init then fails
        # (network, 401/403 auth-invalid, etc.) send_file returns None and the
        # caller's failure branch has a row to flip to "failed" — otherwise
        # the whole send is invisible from history's perspective.
        if on_progress:
            on_progress(transfer_id, 0, chunk_count)

        negotiated_mode = self._init_transfer_with_retry(
            transfer_id, recipient_id, encrypted_meta, chunk_count,
            on_progress, mode=requested_mode,
        )
        if negotiated_mode is None:
            log.error("transfer.init.failed transfer_id=%s", transfer_id[:12])
            return None
        log.info("transfer.init.accepted transfer_id=%s recipient=%s chunks=%d "
                 "requested_mode=%s negotiated_mode=%s",
                 transfer_id[:12], recipient_id[:12], chunk_count,
                 requested_mode, negotiated_mode)

        if negotiated_mode == "streaming":
            return self._upload_stream(
                filepath, transfer_id, chunk_count, base_nonce,
                symmetric_key, on_stream_progress,
            )

        # Classic path — unchanged byte-for-byte from pre-streaming.
        try:
            with open(filepath, "rb") as f:
                for index in range(chunk_count):
                    plaintext = f.read(CHUNK_SIZE)  # last chunk may be short; empty file → b""
                    encrypted = KeyManager.encrypt_chunk(
                        plaintext, base_nonce, index, symmetric_key)
                    err = self._upload_chunk_with_retry(
                        transfer_id, index, chunk_count, encrypted)
                    if err is not None:
                        log.error("transfer.upload.failed transfer_id=%s reason=%s",
                                  transfer_id[:12], err)
                        return None
                    if on_progress:
                        on_progress(transfer_id, index + 1, chunk_count)
        except OSError as e:
            log.error("transfer.upload.failed transfer_id=%s error_kind=%s",
                      transfer_id[:12], type(e).__name__)
            return None

        log.info("transfer.upload.completed transfer_id=%s name=%s",
                 transfer_id[:12], display)
        return transfer_id

    def _upload_stream(self, filepath: Path, transfer_id: str,
                       chunk_count: int, base_nonce: bytes,
                       symmetric_key: bytes,
                       on_stream_progress: callable = None) -> str | None:
        """Streaming upload state machine (see docs/plans/streaming-
        improvement.md §3.1 and desktop-streaming-relay-plan.md §C.4).

        Sequential per-chunk encrypt → upload. Outcomes:
          * UPLOAD_OK         → bump uploaded counter, fire
                                 on_stream_progress(state='sending').
          * UPLOAD_STORAGE_FULL (507) → enter waiting_stream, backoff
                                 per STREAM_QUOTA_BACKOFF_RAMP_S, retry
                                 the SAME chunk. On 30-min expiry →
                                 abort(sender_failed), state='failed'.
          * UPLOAD_ABORTED (410) → recipient_abort from the other side.
                                 State flips to 'aborted'; we do NOT
                                 DELETE (server already wiped).
          * network/other      → reuse the classic 2-min / 5s budget.
                                 On exhaustion → abort(sender_failed),
                                 state='failed'.

        Returns transfer_id on success, None on any terminal failure
        (aborted, failed, quota_timeout).

        On entry we fire on_stream_progress(tid, 0, N, 'sending') so the
        caller can flip the history row off the "uploading 0/N" placeholder
        written just before init.
        """
        # Initial state: uploaded 0 chunks, streaming negotiated. Caller
        # rewrites its row mode + status here.
        if on_stream_progress:
            on_stream_progress(transfer_id, 0, chunk_count, "sending")

        try:
            with open(filepath, "rb") as f:
                for index in range(chunk_count):
                    plaintext = f.read(CHUNK_SIZE)
                    encrypted = KeyManager.encrypt_chunk(
                        plaintext, base_nonce, index, symmetric_key)
                    result = self._upload_stream_chunk(
                        transfer_id, index, chunk_count, encrypted,
                        on_stream_progress,
                    )
                    if result == "ok":
                        if on_stream_progress:
                            on_stream_progress(transfer_id, index + 1,
                                               chunk_count, "sending")
                        continue
                    if result == "aborted":
                        if on_stream_progress:
                            on_stream_progress(transfer_id, index,
                                               chunk_count, "aborted")
                        log.info(
                            "transfer.stream.aborted_by_recipient transfer_id=%s "
                            "chunk_index=%d",
                            transfer_id[:12], index,
                        )
                        return None
                    if result == "failed":
                        # Client-side give-up: network budget exhausted
                        # OR 30-min quota waiting window expired. Tell
                        # the server so the recipient's row can flip to
                        # aborted, then surface failure locally.
                        self.abort_transfer(transfer_id, "sender_failed")
                        if on_stream_progress:
                            on_stream_progress(transfer_id, index,
                                               chunk_count, "failed")
                        log.error(
                            "transfer.upload.failed transfer_id=%s "
                            "chunk_index=%d mode=streaming",
                            transfer_id[:12], index,
                        )
                        return None
                    # Defensive: unknown result — bail like a failure.
                    log.error(
                        "transfer.upload.failed transfer_id=%s "
                        "reason=unknown_upload_result result=%s",
                        transfer_id[:12], result,
                    )
                    self.abort_transfer(transfer_id, "sender_failed")
                    if on_stream_progress:
                        on_stream_progress(transfer_id, index, chunk_count, "failed")
                    return None
        except OSError as e:
            log.error("transfer.upload.failed transfer_id=%s error_kind=%s "
                      "mode=streaming",
                      transfer_id[:12], type(e).__name__)
            self.abort_transfer(transfer_id, "sender_failed")
            if on_stream_progress:
                on_stream_progress(transfer_id, 0, chunk_count, "failed")
            return None

        log.info("transfer.upload.completed transfer_id=%s mode=streaming",
                 transfer_id[:12])
        return transfer_id

    def _upload_stream_chunk(self, transfer_id: str, index: int,
                              chunk_count: int, encrypted: bytes,
                              on_stream_progress: callable = None) -> str:
        """Upload a single streaming chunk with the full streaming retry
        policy. Returns one of 'ok' | 'aborted' | 'failed'.

        Two interleaved budgets:
          * 507 quota waiting: STORAGE_FULL_MAX_WINDOW_S (30 min) of
            continuous 507s → failed. Backoff follows
            STREAM_QUOTA_BACKOFF_RAMP_S (2→4→8→16→30s).
          * Network errors: CHUNK_MAX_FAILURE_WINDOW_S (2 min) of
            continuous non-507, non-410 errors → failed. 5-s cadence.

        Each successful upload (OK) resets both budgets. A non-507
        response also clears the storage-full flag in the connection
        so the tray banner hides.
        """
        quota_waiting_since: float | None = None
        quota_ramp_idx = 0
        network_started_at: float | None = None
        signaled_waiting = False

        while True:
            try:
                outcome = self.upload_chunk(transfer_id, index, encrypted)
            except (requests.RequestException, OSError, ValueError) as e:
                log.warning(
                    "transfer.chunk.failed transfer_id=%s chunk_index=%d "
                    "error_kind=%s",
                    transfer_id[:12], index, type(e).__name__,
                )
                outcome = ChunkUploadOutcome(status=UPLOAD_NETWORK_ERROR)

            if outcome.status == UPLOAD_OK:
                log.debug(
                    "transfer.chunk.uploaded transfer_id=%s chunk_index=%d/%d mode=streaming",
                    transfer_id[:12], index + 1, chunk_count,
                )
                if signaled_waiting:
                    # Drain observed — clear the global storage-full
                    # banner. The row status flips back to 'sending' via
                    # the on_stream_progress call in the caller's loop.
                    self.conn.clear_storage_full()
                return "ok"

            if outcome.status == UPLOAD_ABORTED:
                log.info(
                    "transfer.stream.aborted transfer_id=%s chunk_index=%d reason=%s",
                    transfer_id[:12], index, outcome.abort_reason or "unknown",
                )
                return "aborted"

            if outcome.status == UPLOAD_STORAGE_FULL:
                # Reset network streak (this isn't a network error).
                network_started_at = None
                now = time.monotonic()
                if quota_waiting_since is None:
                    quota_waiting_since = now
                    self.conn.mark_storage_full()
                    if not signaled_waiting and on_stream_progress:
                        # Flip caller's row to waiting_stream on first 507.
                        on_stream_progress(
                            transfer_id, index, chunk_count, "waiting_stream",
                        )
                        signaled_waiting = True
                elapsed = now - quota_waiting_since
                if elapsed >= STORAGE_FULL_MAX_WINDOW_S:
                    log.warning(
                        "transfer.stream.waiting_quota.timed_out "
                        "transfer_id=%s chunk_index=%d elapsed=%ds",
                        transfer_id[:12], index, int(elapsed),
                    )
                    return "failed"
                delay = STREAM_QUOTA_BACKOFF_RAMP_S[
                    min(quota_ramp_idx, len(STREAM_QUOTA_BACKOFF_RAMP_S) - 1)
                ]
                quota_ramp_idx = min(quota_ramp_idx + 1,
                                     len(STREAM_QUOTA_BACKOFF_RAMP_S) - 1)
                log.info(
                    "transfer.stream.waiting_quota transfer_id=%s chunk_index=%d "
                    "retry_in=%.1fs elapsed=%ds",
                    transfer_id[:12], index, delay, int(elapsed),
                )
                time.sleep(delay)
                continue

            # Everything else: network, auth, 404, generic server error.
            # Matches the classic helper's 2-min / 5-s cadence.
            now = time.monotonic()
            if network_started_at is None:
                network_started_at = now
                log.warning(
                    "transfer.chunk.failed transfer_id=%s chunk_index=%d/%d "
                    "status=%s retry_in=%ds",
                    transfer_id[:12], index + 1, chunk_count, outcome.status,
                    int(CHUNK_RETRY_DELAY_S),
                )
            elif now - network_started_at >= CHUNK_MAX_FAILURE_WINDOW_S:
                log.error(
                    "transfer.chunk.failed transfer_id=%s chunk_index=%d "
                    "final_status=%s elapsed=%ds",
                    transfer_id[:12], index, outcome.status,
                    int(now - network_started_at),
                )
                return "failed"
            # Non-507 reset of the quota ramp, but keep the quota_waiting_since
            # null — we already nulled it above if we were in quota mode.
            quota_ramp_idx = 0
            time.sleep(CHUNK_RETRY_DELAY_S)

    def _upload_chunk_with_retry(self, transfer_id: str, index: int,
                                  chunk_count: int, encrypted: bytes) -> str | None:
        """Upload one chunk with 5 s retry cadence. Returns None on success,
        or an error string if the same chunk has been failing continuously
        for longer than CHUNK_MAX_FAILURE_WINDOW_S.

        This path is classic-only: streaming uses a separate loop that
        branches on the full ``ChunkUploadOutcome.status`` enum. Here we
        only care about OK vs not-OK, so any non-OK outcome falls into
        the generic retry bucket. 410 / 507 are not expected on the
        classic path (the server only returns them for streaming
        transfers) but if they do arrive they're treated as a transient
        upload failure and the 2-min budget applies, same as any other.
        """
        first_failure_at: float | None = None
        while True:
            try:
                outcome = self.upload_chunk(transfer_id, index, encrypted)
                if outcome.status == UPLOAD_OK:
                    log.debug("transfer.chunk.uploaded transfer_id=%s chunk_index=%d/%d",
                              transfer_id[:12], index + 1, chunk_count)
                    return None
            except (requests.RequestException, OSError, ValueError) as e:
                log.warning("transfer.chunk.failed transfer_id=%s chunk_index=%d error_kind=%s",
                            transfer_id[:12], index, type(e).__name__)
            now = time.monotonic()
            if first_failure_at is None:
                first_failure_at = now
                log.warning("transfer.chunk.failed transfer_id=%s chunk_index=%d/%d reason=retry_in_%ds",
                            transfer_id[:12], index + 1, chunk_count, int(CHUNK_RETRY_DELAY_S))
            elif now - first_failure_at >= CHUNK_MAX_FAILURE_WINDOW_S:
                return (f"Chunk {index + 1}/{chunk_count} failed continuously "
                        f"for {int(CHUNK_MAX_FAILURE_WINDOW_S)}s")
            time.sleep(CHUNK_RETRY_DELAY_S)

    def _init_transfer_with_retry(self, transfer_id: str, recipient_id: str,
                                   encrypted_meta: str, chunk_count: int,
                                   on_progress: callable = None,
                                   *, mode: str = "classic") -> str | None:
        """Drive init with different retry semantics per failure mode:

        * 507 storage_full: retry indefinitely. The recipient's quota
          is occupied by earlier transfers that will drain as the phone
          downloads them. Caller sees the row in WAITING state. The
          ConnectionManager notes the storage-pressure condition so the
          tray / HomeScreen banner can surface it. (Streaming init
          never returns 507 — the projected-size check is skipped and
          quota is enforced per-chunk instead; see `_upload_stream`.)

        * Network exception / other 5xx: retry on the same 5s cadence
          as chunk upload, capped at CHUNK_MAX_FAILURE_WINDOW_S (2 min),
          then give up — matches the chunk-upload tolerance window.

        * 201 ok: proceed to chunk upload.

        ``on_progress`` signals init-time state via sentinel chunk
        indices — this is the classic path's state channel, kept
        separate from streaming's ``on_stream_progress``:

          * ``(tid, 0, N)``   — initial row placeholder (fired by
                                 send_file before calling this helper).
          * ``(tid, -1, N)``  — 507 storage_full: flip row to WAITING.
                                 Fires once on first 507, then on each
                                 subsequent retry timestamp refresh.
          * ``(tid, -2, N)``  — 413 too_large: terminal, tag row with
                                 failure_reason='too_large'.

        These sentinels pre-date the typed ``on_stream_progress``
        (C.4) and are still live because classic init waiting is a
        distinct flow from streaming mid-stream waiting. They're NOT
        dead code — removing them requires a parallel cleanup of the
        classic callers in windows.py and runners/send_runner.py.
        Left in place; documented here so the contract stays visible.

        Returns the server-negotiated mode string ("classic" or
        "streaming") on success, or None on failure. Callers that only
        need a bool can treat None as False.
        """
        first_failure_at: float | None = None
        waiting_started_at: float | None = None
        signaled_waiting = False
        while True:
            status = "failed"
            negotiated_mode: str | None = None
            try:
                status, negotiated_mode = self.init_transfer(
                    transfer_id, recipient_id, encrypted_meta, chunk_count,
                    mode=mode,
                )
            except (requests.RequestException, OSError, ValueError) as e:
                log.warning("transfer.init.failed transfer_id=%s error_kind=%s",
                            transfer_id[:12], type(e).__name__)
                status = "failed"

            if status == "ok":
                if signaled_waiting:
                    # Release the storage-full flag so the banner clears
                    # the moment this transfer finally lands.
                    self.conn.clear_storage_full()
                return negotiated_mode or "classic"

            if status == "too_large":
                # 413 — no retry. Server's quota is smaller than this
                # single transfer; nothing the client can do but surface
                # the error. Attach the reason via on_progress so the
                # caller can tag the history row.
                log.error("transfer.init.too_large transfer_id=%s", transfer_id[:12])
                if on_progress:
                    try:
                        on_progress(transfer_id, -2, chunk_count)
                    except Exception:
                        log.exception("too_large on_progress failed")
                return None

            if status == "storage_full":
                self.conn.mark_storage_full()
                if not signaled_waiting and on_progress:
                    try:
                        on_progress(transfer_id, -1, chunk_count)
                    except Exception:
                        log.exception("waiting-state on_progress failed")
                    signaled_waiting = True
                    waiting_started_at = time.monotonic()
                # Bounded wait on storage-full: if the recipient quota
                # doesn't free up in STORAGE_FULL_MAX_WINDOW_S (30 min)
                # we give up and surface Failed. Prevents a long-dead
                # send-files subprocess from accidentally keeping the
                # row alive forever.
                if waiting_started_at is not None and \
                        time.monotonic() - waiting_started_at >= STORAGE_FULL_MAX_WINDOW_S:
                    log.warning(
                        "transfer.init.waiting.timed_out transfer_id=%s elapsed=%ds",
                        transfer_id[:12], int(STORAGE_FULL_MAX_WINDOW_S),
                    )
                    return None
                log.info("transfer.init.waiting transfer_id=%s reason=storage_full",
                         transfer_id[:12])
                time.sleep(CHUNK_RETRY_DELAY_S)
                continue

            # status == "failed" — apply 2-min cap.
            now = time.monotonic()
            if first_failure_at is None:
                first_failure_at = now
                log.warning("transfer.init.failed transfer_id=%s reason=retry_in_%ds",
                            transfer_id[:12], int(CHUNK_RETRY_DELAY_S))
            elif now - first_failure_at >= CHUNK_MAX_FAILURE_WINDOW_S:
                return None
            time.sleep(CHUNK_RETRY_DELAY_S)

    def get_stats(self, paired_with: str | None = None) -> dict | None:
        """Get connection statistics from the server."""
        path = "/api/devices/stats"
        if paired_with:
            path += f"?paired_with={paired_with}"
        resp = self.conn.request("GET", path)
        if resp and resp.status_code == 200:
            return resp.json()
        return None

    def ping_device(self, recipient_id: str, timeout: float = 8.0) -> dict | None:
        """Probe paired device liveness. Server sends HIGH FCM and waits up to 5s
        for pong. Returns {online, last_seen_at, rtt_ms, via} or None on failure."""
        resp = self.conn.request(
            "POST", "/api/devices/ping",
            json={"recipient_id": recipient_id},
            timeout=timeout,
        )
        if resp and resp.status_code == 200:
            return resp.json()
        return None

    def get_sent_status(self, timeout: float = 30, *,
                        track_state: bool = True) -> list[dict]:
        """Get delivery status of transfers sent by this device.

        ``track_state=False`` is for the 500 ms delivery-tracker loop where
        a timeout on a single 750 ms poll shouldn't be interpreted as
        "server is down" and shouldn't trigger the exponential backoff
        (which caused a CONNECTED⇄DISCONNECTED thrash under any slow
        network)."""
        resp = self.conn.request("GET", "/api/transfers/sent-status",
                                  timeout=timeout, track_state=track_state)
        if resp and resp.status_code == 200:
            return resp.json().get("transfers", [])
        return []

    # --- Server capability probe -----------------------------------

    def get_capabilities(self, *, force_refresh: bool = False) -> set[str]:
        """Probe GET /api/health for the server's advertised capabilities.

        Returns the set of capability tokens (e.g. ``{"stream_v1"}``).
        Old servers that don't advertise a `capabilities` field return
        the empty set, which is how clients discover streaming is not
        available.

        Result is cached for ``CAPABILITY_CACHE_TTL_S`` (60 s) so the
        streaming sender / receiver can check cheaply per-transfer
        without hammering /api/health. Pass ``force_refresh=True`` to
        bypass the cache (e.g. after a server reconfigure).

        Uses an unauthenticated request so a broken auth token doesn't
        prevent clients from discovering that streaming is unavailable.
        """
        now = time.monotonic()
        cached = getattr(self, "_capabilities_cache", None)
        if not force_refresh and cached is not None:
            caps, expires_at = cached
            if expires_at > now:
                return caps
        caps: set[str] = set()
        try:
            resp = requests.get(
                f"{self.conn.server_url}/api/health",
                timeout=5,
            )
            if resp.status_code == 200:
                body = resp.json()
                raw = body.get("capabilities") if isinstance(body, dict) else None
                if isinstance(raw, list):
                    caps = {c for c in raw if isinstance(c, str)}
        except (requests.RequestException, ValueError):
            # Treat probe failure as "no known capabilities" but do NOT
            # cache the empty result — a transient network blip should
            # not pin us to classic for the next minute.
            return set()
        self._capabilities_cache = (caps, now + CAPABILITY_CACHE_TTL_S)
        return caps

    def supports_streaming(self) -> bool:
        """Convenience shortcut: ``CAPABILITY_STREAM_V1 in get_capabilities()``.

        Streaming senders gate on this before requesting
        ``mode="streaming"`` at init. A False return forces the classic
        path — both for genuinely-old servers and for deployments where
        the operator turned ``streamingEnabled`` off in server config.
        """
        return CAPABILITY_STREAM_V1 in self.get_capabilities()

    # --- Fasttrack: lightweight encrypted message relay ---

    def check_fcm_available(self) -> bool:
        """Check if the server has FCM configured. Uses unauthenticated endpoint."""
        try:
            resp = requests.get(
                f"{self.conn.server_url}/api/fcm/config",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("available", False)
        except requests.RequestException:
            pass
        return False

    def fasttrack_send(self, recipient_id: str, encrypted_data: str) -> int | None:
        """Send an encrypted fasttrack message. Returns message_id or None."""
        log.info("fasttrack.message.send_started recipient=%s size=%d",
                 recipient_id[:12], len(encrypted_data))
        resp = self.conn.request("POST", "/api/fasttrack/send", json={
            "recipient_id": recipient_id,
            "encrypted_data": encrypted_data,
        })
        if resp and resp.status_code == 201:
            msg_id = resp.json().get("message_id")
            log.info("fasttrack.message.send_succeeded message_id=%s", msg_id)
            return msg_id
        log.error("fasttrack.message.send_failed status=%s",
                  resp.status_code if resp else "no_response")
        return None

    def fasttrack_pending(self) -> list[dict]:
        """Fetch pending fasttrack messages for this device."""
        resp = self.conn.request("GET", "/api/fasttrack/pending")
        if resp and resp.status_code == 200:
            msgs = resp.json().get("messages", [])
            if msgs:
                log.debug("fasttrack.message.pending_listed count=%d", len(msgs))
            return msgs
        return []

    def fasttrack_ack(self, message_id: int) -> bool:
        """Acknowledge and delete a fasttrack message."""
        resp = self.conn.request("POST", f"/api/fasttrack/{message_id}/ack")
        ok = resp is not None and resp.status_code == 200
        log.debug("fasttrack.message.acked message_id=%d outcome=%s",
                  message_id, "succeeded" if ok else "failed")
        return ok
