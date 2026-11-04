"""High-level send_file orchestrator.

Encrypts and uploads a file to a recipient using a chunked pipeline:
read one chunk → encrypt → upload → repeat. Memory is bounded by
CHUNK_SIZE regardless of file size. Picks classic vs. streaming based
on capability + filename + caller hint, then dispatches.
"""

import logging
import math
import uuid
from pathlib import Path

from ..crypto import CHUNK_SIZE, KeyManager

log = logging.getLogger(__name__)


class TransfersSendMixin:
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
