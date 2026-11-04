"""Per-row delete + sender/recipient abort flow.

``do_local_remove`` is the shared shrink+fade + ``history.remove`` path
called by every terminal delete branch. ``on_delete`` is the trash
button handler — it decides whether the row still owns server state
(needs an abort confirmation) before delegating to ``do_local_remove``.

The 250 ms shrink+fade animation here is load-bearing — see CLAUDE.md
"swipe-to-delete animated (250 ms shrink+fade)". The 300 ms
``GLib.timeout_add`` matches the CSS transition window in the
``.transfer-card.removing`` rule (window.py).
"""

from __future__ import annotations

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from ..api_client import ApiClient
from ..connection import ConnectionManager
from ..history import TransferStatus
from .context import HistoryContext
from .status import row_key as _row_key


def do_local_remove(ctx: HistoryContext, it, c, b) -> None:
    """The shared shrink+fade + history.remove path. Does NOT
    call the server; callers decide whether to cancel first."""
    ctx.history.remove(it)
    ctx.row_widgets.pop(_row_key(it), None)
    if c in ctx.all_widgets:
        ctx.all_widgets.remove(c)
    ctx.structural_sig[0] = None
    b.set_sensitive(False)
    c.add_css_class("removing")

    list_container = ctx.list_container

    def _finalize():
        try:
            list_container.remove(c)
        except Exception:
            pass
        return False
    GLib.timeout_add(300, _finalize)


def on_delete(ctx: HistoryContext, b, it, c) -> None:
    # Terminal rows (delivered sent, completed received,
    # aborted, failed) have no server state left — the
    # click just prunes the local history entry.
    # Non-terminal rows still own bytes on the server:
    #   * Sent + not-yet-delivered
    #       -> abort as sender. Recipient's next chunk call
    #         returns 410 and their row flips to Aborted.
    #   * Received + still downloading (streaming only —
    #                 classic receivers finalise before
    #                 returning to the history list)
    #       -> abort as recipient. Sender's next chunk
    #         upload returns 410 and their row flips to
    #         Aborted. Poller's streaming download loop
    #         also picks up the 410 on its next GET.
    status = it.get("status", "complete")
    direction = it.get("direction", "sent")
    delivered = it.get("delivered", False)
    is_live_receiver = (
        direction == "received"
        and status == TransferStatus.DOWNLOADING
    )
    is_live_sender = (
        direction == "sent"
        and not delivered
        and status not in (TransferStatus.FAILED,
                           TransferStatus.ABORTED)
    )
    if not (is_live_sender or is_live_receiver):
        do_local_remove(ctx, it, c, b)
        return

    tid = it.get("transfer_id")
    label = ctx.history.get_label(it)
    if is_live_receiver:
        heading = "Stop receiving?"
        body = (f"The download of “{label}” will be "
                f"cancelled and the sender will see Aborted.")
        action_label = "Stop download"
        abort_reason = "recipient_abort"
    else:
        heading = "Cancel delivery?"
        body = (f"The recipient will no longer receive "
                f"“{label}”.")
        action_label = "Cancel delivery"
        abort_reason = "sender_abort"

    win = ctx.win
    config = ctx.config
    crypto = ctx.crypto

    dialog = Adw.MessageDialog(
        transient_for=win,
        heading=heading,
        body=body,
    )
    dialog.add_response("keep", "Keep")
    dialog.add_response("cancel", action_label)
    dialog.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("keep")
    dialog.set_close_response("keep")

    def on_response(d, response, _it=it, _c=c, _b=b, _tid=tid,
                    _reason=abort_reason):
        if response != "cancel":
            return
        # Fire the server abort in a worker thread so the
        # UI stays responsive if the server is slow; the
        # local shrink/fade starts immediately either way
        # (abort success or network failure — the row goes
        # either way, the server will gc on its own expiry
        # if we can't reach it now).
        if _tid:
            def _abort_worker(tid_local=_tid, reason=_reason):
                try:
                    conn = ConnectionManager(
                        config.server_url,
                        config.device_id,
                        config.auth_token,
                    )
                    ApiClient(conn, crypto).abort_transfer(
                        tid_local, reason)
                except Exception:
                    pass  # best effort; row still removed locally
            threading.Thread(target=_abort_worker, daemon=True).start()
        do_local_remove(ctx, _it, _c, _b)

    dialog.connect("response", on_response)
    dialog.present()
