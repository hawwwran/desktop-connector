"""Per-chunk upload / download / ACK against the relay."""

import logging
import time

import requests

from .constants import (
    CHUNK_MAX_FAILURE_WINDOW_S,
    CHUNK_RETRY_DELAY_S,
    DOWNLOAD_ABORTED,
    DOWNLOAD_AUTH_ERROR,
    DOWNLOAD_FAILED,
    DOWNLOAD_NETWORK_ERROR,
    DOWNLOAD_NOT_FOUND,
    DOWNLOAD_OK,
    DOWNLOAD_TOO_EARLY,
    UPLOAD_ABORTED,
    UPLOAD_AUTH_ERROR,
    UPLOAD_FAILED,
    UPLOAD_NETWORK_ERROR,
    UPLOAD_NOT_FOUND,
    UPLOAD_OK,
    UPLOAD_STORAGE_FULL,
)
from .outcomes import ChunkDownloadOutcome, ChunkUploadOutcome
from .parsing import _extract_abort_reason, _parse_retry_after_ms

log = logging.getLogger(__name__)


class TransfersChunksMixin:
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
