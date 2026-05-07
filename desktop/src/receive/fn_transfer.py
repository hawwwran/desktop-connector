"""``.fn.*`` command-style transfer dispatch + receive-side message handlers.

Once the classic .fn payload has been written to disk, ``_handle_fn_transfer``
parses it via the unified message dispatcher and routes to the per-message
handlers below (clipboard text/image, unpair). Files are unlinked after
dispatch — they exist only as the protocol envelope.
"""

import logging
import time
from pathlib import Path

from ..file_manager_integration import sync_file_manager_targets
from ..messaging import FnTransferAdapter, MessageType
from ..receive_actions import (
    ReceiveActionBatch,
    apply_receive_text_actions,
    extract_received_urls,
)
from .clipboard_image import _clipboard_image_filename

log = logging.getLogger(__name__)


class FnTransferMixin:
    def _handle_fn_transfer(
        self,
        filepath: Path,
        *,
        sender_id: str | None = None,
        transfer_id: str = "",
        mime_type: str = "application/octet-stream",
        receive_action_batch: ReceiveActionBatch | None = None,
    ) -> None:
        """Handle special .fn. transfers through the unified message dispatcher."""
        name = filepath.name
        try:
            message = FnTransferAdapter.to_device_message(name, filepath.read_bytes(), sender_id=sender_id)
            if message is None:
                log.warning("fasttrack.command.unknown fn=%s", name)
                return
            if sender_id:
                self._mark_active_device(sender_id, reason="incoming")

            if message.type == MessageType.CLIPBOARD_TEXT:
                self._handle_message_clipboard_text(
                    message,
                    receive_action_batch=receive_action_batch,
                )
                return

            if message.type == MessageType.CLIPBOARD_IMAGE:
                self._handle_message_clipboard_image(
                    message,
                    source_path=filepath,
                    sender_id=sender_id,
                    transfer_id=transfer_id,
                    mime_type=mime_type,
                    receive_action_batch=receive_action_batch,
                )
                return

            if not self._message_dispatcher.dispatch(message):
                log.warning("fasttrack.command.unknown type=%s", message.type.value)
        except Exception as e:
            log.error("command.dispatch.failed filename=%s error_kind=%s", name, type(e).__name__)
        finally:
            filepath.unlink(missing_ok=True)

    def _handle_message_clipboard_text(
        self,
        message,
        *,
        receive_action_batch: ReceiveActionBatch | None = None,
    ) -> None:
        text = str(message.payload.get("text", ""))

        urls = extract_received_urls(text)
        preview = text if len(urls) == 1 else (text[:60] + "..." if len(text) > 60 else text)
        self.history.add(filename=message.metadata.get("filename", ".fn.clipboard.text"),
                         display_label=preview, direction="received", size=len(text),
                         sender_id=message.sender_id or "",
                         peer_device_id=message.sender_id or "")
        result = apply_receive_text_actions(
            self.config,
            self.platform,
            text,
            limiter=self._receive_action_limiter,
            batch=receive_action_batch,
        )
        if not result.ok:
            log.warning("receive_action.text.failed length=%d", len(text))
        # Suppress the "Clipboard received" toast when a configured
        # action already gave the user feedback (browser opened,
        # clipboard updated). The action effect is the notification.
        if not result.action_ran:
            self.platform.notifications.notify("Clipboard received", preview[:60])

    def _handle_message_clipboard_image(
        self,
        message,
        *,
        source_path: Path | None = None,
        sender_id: str | None = None,
        transfer_id: str = "",
        mime_type: str = "application/octet-stream",
        receive_action_batch: ReceiveActionBatch | None = None,
    ) -> None:
        data = message.payload.get("image_bytes", b"")
        if not isinstance(data, (bytes, bytearray)):
            log.warning("clipboard.image.save_failed reason=invalid_payload")
            return

        image_bytes = bytes(data)
        save_dir = Path(self.config.save_directory)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Cannot create save directory for clipboard image")
            return

        temp_path = source_path
        if temp_path is None:
            parts_dir = save_dir / ".parts"
            try:
                parts_dir.mkdir(parents=True, exist_ok=True)
                temp_path = parts_dir / f".incoming_clipboard_image_{time.monotonic_ns()}.part"
                temp_path.write_bytes(image_bytes)
            except OSError:
                log.exception("Failed to stage clipboard image")
                return

        filename = _clipboard_image_filename(mime_type, image_bytes)
        final_path = self._finalize_temp_to_unique(temp_path, save_dir, filename)
        if final_path is None:
            log.warning("clipboard.image.save_failed")
            return

        final_size = final_path.stat().st_size
        log.info("clipboard.image.saved bytes=%d name=%s", final_size, final_path.name)
        self.history.add(
            filename=final_path.name,
            display_label=final_path.name,
            direction="received",
            size=final_size,
            content_path=str(final_path),
            sender_id=sender_id or "",
            peer_device_id=sender_id or message.sender_id or "",
            transfer_id=transfer_id,
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

    def _handle_message_unpair(self, message) -> None:
        peer_id = message.sender_id
        if not peer_id:
            log.warning("pairing.unpair.ignored reason=missing_sender")
            return
        log.info("pairing.unpair.received peer=%s", peer_id[:12])
        self.config.remove_paired_device(peer_id)
        try:
            sync_file_manager_targets(self.config)
        except Exception:
            log.debug("pairing.unpair.file_manager_sync_failed", exc_info=True)
        try:
            self.platform.notifications.notify(
                "Unpaired",
                "Paired device disconnected",
            )
        except Exception:
            log.exception("notification during sender-scoped unpair failed")
