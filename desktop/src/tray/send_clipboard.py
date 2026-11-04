"""One-shot "Send Clipboard" tray action.

Reads the system clipboard (text or image) via the platform backend,
encrypts + uploads it as a regular transfer to the active paired
device, and surfaces success/failure as a notification. Thread-spawned
from the menu callback so the menu stays responsive.
"""

import base64
import logging
import re
import tempfile
import threading
from pathlib import Path

from ..devices import ConnectedDeviceRegistry, short_device_id

log = logging.getLogger(__name__)


class SendClipboardMixin:
    def _send_clipboard(self, *_) -> None:
        threading.Thread(target=self._do_send_clipboard, daemon=True).start()

    def _do_send_clipboard(self) -> None:
        result = self.platform.clipboard.read_clipboard()
        if result is None:
            self.platform.notifications.notify("Clipboard empty", "Nothing to send")
            return

        filename, data, mime_type = result
        registry = ConnectedDeviceRegistry(self.config)
        target = registry.get_active_device()
        if target is None:
            return
        if not target.symmetric_key_b64:
            log.error(
                "clipboard.send.failed reason=missing_pairing_key peer=%s",
                target.short_id,
            )
            self.platform.notifications.notify(
                "Send failed",
                "Missing pairing key for the selected device",
            )
            return

        target_id = target.device_id
        symmetric_key = base64.b64decode(target.symmetric_key_b64)
        try:
            registry.mark_active(target_id, reason="outgoing")
        except Exception:
            log.debug(
                "device.active.update_failed peer=%s reason=outgoing",
                short_device_id(target_id),
                exc_info=True,
            )

        tmp = Path(tempfile.mktemp(suffix="_" + filename))
        tmp.write_bytes(data)

        if mime_type.startswith("text/"):
            text = data.decode("utf-8", errors="replace")
            urls = re.findall(r'https?://\S+', text)
            if len(urls) == 1:
                preview = text
            elif len(text) > 40:
                preview = text[:40] + "..."
            else:
                preview = text
        else:
            preview = "Clipboard image"

        # Add to history before uploading so it appears immediately
        progress_tid = [None]
        def upload_progress(transfer_id, uploaded, total_chunks):
            if uploaded == 0:
                progress_tid[0] = transfer_id
                self.history.add(filename=filename, display_label=preview,
                                 direction="sent", size=len(data), content_path=str(tmp),
                                 transfer_id=transfer_id, status="uploading",
                                 chunks_downloaded=0, chunks_total=total_chunks,
                                 peer_device_id=target_id)
            else:
                self.history.update(transfer_id,
                                    chunks_downloaded=uploaded, chunks_total=total_chunks)

        tid = self.api.send_file(tmp, target_id, symmetric_key,
                                 filename_override=filename, on_progress=upload_progress)
        if tid:
            # Never log the preview — it's decrypted clipboard content.
            log.info("Clipboard sent (len=%d)", len(preview))
            self.platform.notifications.notify("Clipboard sent", preview)
            # Upload logic cleans up its own progress fields; delivery tracker owns recipient_* from here.
            self.history.update(tid, status="complete", chunks_downloaded=0, chunks_total=0)
        else:
            if progress_tid[0]:
                self.history.update(progress_tid[0], status="failed")
            self.platform.notifications.notify("Send failed", "Could not send clipboard")
