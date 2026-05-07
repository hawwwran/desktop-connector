"""Classic-mode (store-then-forward) download path.

Pulls all chunks of a transfer to a temp file, finalizes atomically,
ACKs the transfer. Memory bounded by CHUNK_SIZE; file kept on ACK
failure so a network blip after a fully-received file doesn't lose it
(the sender will eventually time out delivery, but the bytes are safe).

Also owns the ``.fn.*`` command-style branch (tiny in-memory payload)
and the startup sweep of orphaned ``.incoming_*.part`` files left
behind by aborted receives (force-quit, OOM, power loss).
"""

import logging
import os
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag

from ..api_client import DOWNLOAD_OK
from ..crypto import KeyManager
from ..receive_actions import ReceiveActionBatch

log = logging.getLogger(__name__)

# Orphan partials (.incoming_*.part) older than this are swept on poller start.
# Matches Android's STALE_PART_TTL_MS.
STALE_PART_TTL_S = 24 * 60 * 60

# Per-chunk download retry policy (kept simple and local; mirrors current behavior).
CHUNK_DOWNLOAD_ATTEMPTS = 3


class ClassicDownloadMixin:
    def _receive_fn_transfer(self, transfer_id: str, sender_id: str, filename: str,
                             chunk_count: int, symmetric_key: bytes, *,
                             mime_type: str = "application/octet-stream",
                             receive_action_batch: ReceiveActionBatch | None = None) -> None:
        """Handle command-style .fn.* transfers: tiny payloads, in-memory path,
        write to a tmp file under the save_dir, dispatch, ACK."""
        plaintext_parts: list[bytes] = []
        for i in range(chunk_count):
            # .fn transfers are always classic (streaming is forbidden for
            # them per the plan), so the only outcomes we care about are
            # "200 with bytes" vs "anything else → bail". The streaming
            # 425/410 paths are handled in the file-transfer branch.
            outcome = self.api.download_chunk(transfer_id, i)
            if outcome.status != DOWNLOAD_OK or outcome.data is None:
                log.error("Failed to download .fn chunk %d/%d of transfer %s status=%s",
                          i + 1, chunk_count, transfer_id[:12], outcome.status)
                return
            try:
                plaintext_parts.append(KeyManager.decrypt_chunk(outcome.data, symmetric_key))
            except Exception:
                log.exception("Failed to decrypt .fn chunk %d of transfer %s",
                              i, transfer_id[:12])
                return

        save_dir = Path(self.config.save_directory)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Cannot create save directory for .fn transfer")
            return

        # .fn payload is consumed and unlinked immediately by _handle_fn_transfer.
        save_path = save_dir / filename
        try:
            save_path.write_bytes(b"".join(plaintext_parts))
        except OSError:
            log.exception("Failed to write .fn payload for %s", transfer_id[:12])
            return

        self._handle_fn_transfer(
            save_path,
            sender_id=sender_id,
            transfer_id=transfer_id,
            mime_type=mime_type,
            receive_action_batch=receive_action_batch,
        )

        if self.api.ack_transfer(transfer_id):
            log.info("delivery.acked transfer_id=%s", transfer_id[:12])
        else:
            log.warning("delivery.acked transfer_id=%s reason=server_rejected",
                        transfer_id[:12])

    def _receive_file_transfer(self, transfer_id: str, sender_id: str,
                               filename: str, chunk_count: int,
                               base_nonce: bytes, symmetric_key: bytes, *,
                               receive_action_batch: ReceiveActionBatch | None = None) -> None:
        """Stream chunks to a temp file under {save_dir}/.parts/, then
        atomic-finalize to the destination via os.link (race-free) +
        unlink. Memory bounded by CHUNK_SIZE. ACK is sent only after the
        durable finalize; the file is kept on ACK failure (sender will
        eventually time out delivery)."""
        # History row first, with 0/N progress so the bar appears immediately.
        self.history.add(
            filename=filename,
            display_label=filename,
            direction="received",
            size=0,
            transfer_id=transfer_id,
            sender_id=sender_id,
            status="downloading",
            chunks_downloaded=0,
            chunks_total=chunk_count,
            peer_device_id=sender_id,
        )

        try:
            save_dir = self.config.save_directory  # property mkdirs the dir
            parts_dir = save_dir / ".parts"
            parts_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Cannot prepare save / .parts directories")
            self.history.update(transfer_id, status="failed",
                                chunks_downloaded=0, chunks_total=0)
            return

        temp_path = parts_dir / f".incoming_{transfer_id}.part"

        success = False
        try:
            # `wb` truncates any stale partial from a prior aborted attempt.
            with open(temp_path, "wb") as out:
                for i in range(chunk_count):
                    plaintext = self._download_and_decrypt_chunk(
                        transfer_id, i, chunk_count, symmetric_key)
                    if plaintext is None:
                        self.history.update(transfer_id, status="failed",
                                            chunks_downloaded=0, chunks_total=0)
                        return
                    out.write(plaintext)
                    self.history.update(transfer_id, chunks_downloaded=i + 1)
                out.flush()
                os.fsync(out.fileno())
            success = True
        except OSError:
            log.exception("Streaming write failed for %s", transfer_id[:12])
            self.history.update(transfer_id, status="failed",
                                chunks_downloaded=0, chunks_total=0)
            return
        finally:
            if not success:
                self._delete_quietly(temp_path)

        final_path = self._finalize_temp_to_unique(temp_path, save_dir, filename)
        if final_path is None:
            self.history.update(transfer_id, status="failed",
                                chunks_downloaded=0, chunks_total=0)
            return

        final_size = final_path.stat().st_size
        log.info("transfer.download.completed transfer_id=%s bytes=%d name=%s",
                 transfer_id[:12], final_size, final_path.name)

        # ACK-after-durable-write: the file is on disk under its final name.
        # If ACK fails the sender will eventually stop seeing "delivering",
        # but we MUST NOT delete a fully received file just because the
        # network hiccupped before we could tell the server.
        ack_ok = self.api.ack_transfer(transfer_id)
        if ack_ok:
            log.info("delivery.acked transfer_id=%s", transfer_id[:12])
        else:
            log.warning("delivery.acked transfer_id=%s reason=keeping_file_after_ack_failure",
                        transfer_id[:12])

        # Download logic cleans up its own progress fields on completion.
        self.history.update(
            transfer_id,
            status="complete",
            size=final_size,
            content_path=str(final_path),
            delivered=True,
            chunks_downloaded=0,
            chunks_total=0,
        )
        action_ran = self._apply_receive_file_action(
            final_path,
            receive_action_batch=receive_action_batch,
        )
        if not action_ran:
            try:
                self.platform.notifications.notify_file_received(final_path)
            except Exception:
                log.exception("notify_file_received failed")
        for cb in self._on_file_received:
            try:
                cb(final_path)
            except Exception:
                log.exception("File received callback error")

    def _download_and_decrypt_chunk(self, transfer_id: str, index: int,
                                     chunk_count: int, symmetric_key: bytes) -> bytes | None:
        """Download + decrypt one chunk. Retries the download on either a
        missing body or an AES-GCM auth failure (InvalidTag). The
        latter defends against server-side races where a concurrent upload
        causes the reader to see partial bytes — the server's atomic rename
        is the primary fix; this is belt-and-suspenders. Returns plaintext
        bytes or None if the chunk cannot be recovered after 3 attempts.

        Classic-mode helper. The streaming receive loop (C.3) handles
        425 / 410 directly and does not route through here.
        """
        for attempt in range(1, CHUNK_DOWNLOAD_ATTEMPTS + 1):
            outcome = self.api.download_chunk(transfer_id, index)
            if outcome.status != DOWNLOAD_OK or outcome.data is None:
                log.warning("Chunk %d/%d download returned no body (attempt %d/%d) status=%s",
                            index + 1, chunk_count, attempt, CHUNK_DOWNLOAD_ATTEMPTS,
                            outcome.status)
            else:
                try:
                    return KeyManager.decrypt_chunk(outcome.data, symmetric_key)
                except InvalidTag:
                    log.warning("Chunk %d/%d decrypt failed (attempt %d/%d), "
                                "re-downloading", index + 1, chunk_count,
                                attempt, CHUNK_DOWNLOAD_ATTEMPTS)
            if attempt < CHUNK_DOWNLOAD_ATTEMPTS:
                time.sleep(2.0 * attempt)
        log.error("Chunk %d/%d failed after %d attempts on %s",
                  index + 1, chunk_count, CHUNK_DOWNLOAD_ATTEMPTS,
                  transfer_id[:12])
        return None

    def _sweep_stale_parts(self, save_dir: Path) -> None:
        """Delete orphaned .incoming_*.part files left behind by aborted
        receives (force-quit, OOM, power loss). Runs once on poller start."""
        parts_dir = save_dir / ".parts"
        if not parts_dir.is_dir():
            return
        cutoff = time.time() - STALE_PART_TTL_S
        removed = 0
        try:
            for entry in parts_dir.iterdir():
                if not entry.name.startswith(".incoming_"):
                    continue
                if not entry.name.endswith(".part"):
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            log.warning("Parts sweep failed", exc_info=True)
            return
        if removed:
            log.info("Cleaned up %d stale .part file(s)", removed)
