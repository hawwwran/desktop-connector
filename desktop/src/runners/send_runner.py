"""Send-file startup runner."""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

from ..api_client import ApiClient
from ..config import Config
from ..connection import ConnectionManager
from ..crypto import KeyManager

log = logging.getLogger("desktop-connector")


def run_send_file(config: Config, crypto: KeyManager, filepath: Path) -> int:
    """Send a single file and exit. Returns 0 on success, 1 on failure."""
    if not config.is_registered:
        log.error("Not registered. Run without --send first to register and pair.")
        return 1
    if not config.is_paired:
        log.error("No paired device. Run with --pair first.")
        return 1

    if not filepath.exists():
        log.error("File not found: %s", filepath)
        return 1

    if filepath.is_dir():
        log.error("send.rejected reason=is_directory path=%s", filepath)
        from ..notifications import notify
        notify(
            "Folder transport is not supported",
            "Send individual files instead.",
        )
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

    try:
        config.active_device_id = target_id
        log.info("device.active.changed peer=%s reason=outgoing", target_id[:8])
    except Exception:
        log.debug(
            "device.active.update_failed peer=%s reason=outgoing",
            target_id[:8],
            exc_info=True,
        )

    from ..history import TransferHistory, TransferStatus

    history = TransferHistory(config.config_dir)
    file_size = filepath.stat().st_size

    state = {
        "tid": None,
        "saw_waiting_classic": False,
        "saw_waiting_stream": False,
        "saw_too_large": False,
        "stream_terminal": False,
    }

    def upload_progress(transfer_id, uploaded, total_chunks):
        """Classic-path callback. Same sentinel semantics as the
        send-files window — -2 is init 413, -1 is init 507, 0 is
        initial row creation, positive values advance the bar."""
        if uploaded == -2:
            state["saw_too_large"] = True
            if state["tid"] is None:
                state["tid"] = transfer_id
                history.add(
                    filename=filepath.name, display_label=filepath.name,
                    direction="sent", size=file_size,
                    content_path=str(filepath), transfer_id=transfer_id,
                    status=TransferStatus.FAILED,
                    chunks_downloaded=0, chunks_total=total_chunks,
                    peer_device_id=target_id,
                    failure_reason="too_large",
                )
            else:
                history.update(transfer_id,
                               status=TransferStatus.FAILED,
                               failure_reason="too_large")
            return
        if uploaded in (0, -1):
            if state["tid"] is None:
                state["tid"] = transfer_id
                history.add(
                    filename=filepath.name, display_label=filepath.name,
                    direction="sent", size=file_size,
                    content_path=str(filepath), transfer_id=transfer_id,
                    status=(TransferStatus.WAITING if uploaded == -1
                            else TransferStatus.UPLOADING),
                    chunks_downloaded=0, chunks_total=total_chunks,
                    peer_device_id=target_id,
                )
            elif uploaded == -1:
                history.update(transfer_id, status=TransferStatus.WAITING)
            else:
                history.update(transfer_id, status=TransferStatus.UPLOADING)
            if uploaded == -1:
                state["saw_waiting_classic"] = True
                history.update(transfer_id,
                               waiting_started_at=int(time.time()))
        else:
            history.update(transfer_id,
                           status=TransferStatus.UPLOADING,
                           chunks_downloaded=uploaded,
                           chunks_total=total_chunks)

    def stream_progress(transfer_id, uploaded, total_chunks, stream_state):
        """Streaming-path callback. See show_send_files in windows.py
        for rationale — mirrored here so the --send one-shot produces
        the same history shape as the GUI flow."""
        if stream_state == "sending":
            history.update(
                transfer_id,
                status=TransferStatus.SENDING,
                mode="streaming",
                chunks_uploaded=uploaded,
                chunks_downloaded=uploaded,
                chunks_total=total_chunks,
            )
        elif stream_state == "waiting_stream":
            state["saw_waiting_stream"] = True
            history.update(
                transfer_id,
                status=TransferStatus.WAITING_STREAM,
                mode="streaming",
                chunks_uploaded=uploaded,
                chunks_total=total_chunks,
                waiting_started_at=int(time.time()),
            )
        elif stream_state == "aborted":
            state["stream_terminal"] = True
            history.update(
                transfer_id,
                status=TransferStatus.ABORTED,
                abort_reason="recipient_abort",
                chunks_downloaded=0,
                chunks_total=0,
            )
        elif stream_state == "failed":
            state["stream_terminal"] = True
            reason = ("quota_timeout" if state["saw_waiting_stream"] else None)
            fields = {
                "status": TransferStatus.FAILED,
                "chunks_downloaded": 0,
                "chunks_total": 0,
            }
            if reason:
                fields["failure_reason"] = reason
            history.update(transfer_id, **fields)

    tid = api.send_file(filepath, target_id, symmetric_key,
                        on_progress=upload_progress,
                        on_stream_progress=stream_progress)
    if tid:
        history.update(tid, status=TransferStatus.COMPLETE,
                       chunks_downloaded=0, chunks_total=0)
        log.info("File sent successfully")
        return 0

    if (state["tid"] and not state["saw_too_large"]
            and not state["stream_terminal"]):
        reason = ("quota_timeout"
                  if state["saw_waiting_classic"] else None)
        fields = {"status": TransferStatus.FAILED}
        if reason:
            fields["failure_reason"] = reason
        history.update(state["tid"], **fields)
    log.error("Failed to send file")
    return 1
