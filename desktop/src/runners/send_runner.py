"""Send-file startup runner."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from ..api_client import ApiClient
from ..config import Config
from ..connection import ConnectionManager
from ..crypto import KeyManager

log = logging.getLogger("desktop-connector")


def run_send_file(config: Config, crypto: KeyManager, filepath: Path) -> int:
    """Send a single file and exit. Returns 0 on success, 1 on failure."""
    if not config.is_registered:
        log.error("Not registered. Run without --send-photo first to register and pair.")
        return 1
    if not config.is_paired:
        log.error("No paired device. Run with --pair first.")
        return 1

    if not filepath.exists():
        log.error("File not found: %s", filepath)
        return 1

    # Get first paired device
    target_id, target_info = config.get_first_paired_device()
    symmetric_key = base64.b64decode(target_info["symmetric_key_b64"])

    conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
    api = ApiClient(conn, crypto)

    # Check connection first
    if not conn.check_connection():
        log.error("Cannot reach server at %s", config.server_url)
        return 1

    from ..history import TransferHistory

    history = TransferHistory(config.config_dir)
    file_size = filepath.stat().st_size

    progress_tid = [None]

    def upload_progress(transfer_id, uploaded, total_chunks):
        if uploaded == 0:
            progress_tid[0] = transfer_id
            history.add(
                filename=filepath.name,
                display_label=filepath.name,
                direction="sent",
                size=file_size,
                content_path=str(filepath),
                transfer_id=transfer_id,
                status="uploading",
                chunks_downloaded=0,
                chunks_total=total_chunks,
            )
        else:
            history.update(
                transfer_id, chunks_downloaded=uploaded, chunks_total=total_chunks
            )

    tid = api.send_file(filepath, target_id, symmetric_key, on_progress=upload_progress)
    if tid:
        # Upload logic cleans up its own progress fields; delivery tracker owns recipient_* from here.
        history.update(tid, status="complete", chunks_downloaded=0, chunks_total=0)
        log.info("File sent successfully")
        return 0

    if progress_tid[0]:
        history.update(progress_tid[0], status="failed")
    log.error("Failed to send file")
    return 1
