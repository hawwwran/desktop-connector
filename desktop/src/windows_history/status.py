"""Per-item status text + progress-bar state for the History window.

``compute_status`` returns ``(text, (show, css_class, fraction))``.
``row_key`` is the diff-stable identifier used by ``build_list``'s
structural/progress signature tuples.
"""

from __future__ import annotations

from typing import Any, Tuple

from ..brand import (
    DC_BLUE_400,
    DC_BLUE_500,
    DC_ORANGE_700,
    DC_YELLOW_500,
)
from ..history import TransferStatus


def compute_status(item: dict) -> Tuple[str, Tuple[bool, Any, float]]:
    item_status = item.get("status", "complete")
    chunks_dl = item.get("chunks_downloaded", 0)
    chunks_total = item.get("chunks_total", 0)
    chunks_up = item.get("chunks_uploaded", 0)
    recv_dl = item.get("recipient_chunks_downloaded", 0)
    recv_total = item.get("recipient_chunks_total", 0)
    delivered = item.get("delivered", False)
    # Streaming "Sending X→Y" and "Waiting X→Y" both need the
    # same numerator/denominator pair: X = sender's upload count,
    # Y = recipient's ack count, N = transfer chunk count. On a
    # row that hasn't had delivery observations yet, fall back
    # to the classic chunks_total so the label isn't "X→0/0".
    stream_total = recv_total or chunks_total
    stream_xy = f"{chunks_up}→{recv_dl}"  # "X→Y"

    # chunks_dl < 0 is the legacy "waiting" sentinel written by
    # earlier builds before status="waiting" existed. Normalise
    # on read so old history rows render as Waiting too, not
    # as "Uploading -1/N".
    if item_status == TransferStatus.WAITING or chunks_dl < 0:
        # Yellow — queued, server storage is full for recipient.
        # Matches the tray/banner "storage full" colour family.
        text = f'<span foreground="{DC_YELLOW_500}">Waiting</span>'
    elif item_status == TransferStatus.WAITING_STREAM:
        # Mid-stream 507: sender paused until quota drains.
        # Same yellow as classic WAITING; different denominator
        # shape (X→Y, not N/total) because we're past init.
        text = (
            f'<span foreground="{DC_YELLOW_500}">Waiting {stream_xy}'
            f'/{stream_total}</span>'
            if stream_total > 0
            else f'<span foreground="{DC_YELLOW_500}">Waiting</span>'
        )
    elif item_status == TransferStatus.ABORTED:
        # Either-party abort. Orange matches the brand error
        # slot — terminal, no retry. abort_reason (optional)
        # is a short tag ("sender_abort", "recipient_abort",
        # "sender_failed") from the DELETE call that surfaced
        # the abort on this side.
        reason = item.get("abort_reason")
        reason_label = {
            "sender_abort": "sender cancelled",
            "recipient_abort": "recipient cancelled",
            "sender_failed": "sender gave up",
        }.get(reason)
        if reason_label:
            text = f'<span foreground="{DC_ORANGE_700}">Aborted ({reason_label})</span>'
        else:
            text = f'<span foreground="{DC_ORANGE_700}">Aborted</span>'
    elif item_status == TransferStatus.UPLOADING:
        text = f"Uploading {chunks_dl}/{chunks_total}" if chunks_total > 0 else "Uploading"
    elif item_status == TransferStatus.SENDING:
        # The SENDING status is a "streaming in-flight" marker
        # set at init time by the sender loop. The LABEL we show
        # depends on where we actually are in the stream:
        #
        #   1. No recipient progress yet   → "Uploading X/N"
        #      (same as classic UPLOADING; sender is the only
        #      active party. Avoids the "Sending 5→0/5" footer
        #      that was confusing users into thinking they'd
        #      already finished.)
        #   2. Real overlap, upload in
        #      progress + recipient acking → "Sending X→Y/N"
        #      (blue; both sides active.)
        #   3. Upload done, recipient
        #      still draining              → "Delivering Y/N"
        #      (blue; matches classic delivery label — only
        #      the recipient is active.)
        #
        # Terminal "Delivered" handled by the `delivered` flag
        # branch further down.
        upload_done = stream_total > 0 and chunks_up >= stream_total
        if stream_total == 0:
            text = f'<span foreground="{DC_BLUE_500}">Sending</span>'
        elif recv_dl == 0:
            text = f"Uploading {chunks_up}/{stream_total}"
        elif not upload_done:
            text = (
                f'<span foreground="{DC_BLUE_500}">Sending {stream_xy}'
                f'/{stream_total}</span>'
            )
        else:
            text = (
                f'<span foreground="{DC_BLUE_500}">Delivering {recv_dl}'
                f'/{stream_total}</span>'
            )
    elif item_status == TransferStatus.DOWNLOADING:
        text = f"Downloading {chunks_dl}/{chunks_total}" if chunks_total > 0 else "Downloading"
    elif item_status == TransferStatus.FAILED:
        # Brand error slot — matches android/server/tray.
        # failure_reason (optional) is a short tag set by the
        # callers that know WHY a send failed; renders as a
        # parenthetical note. No tag => plain "Failed".
        reason = item.get("failure_reason")
        reason_label = {
            "quota": "quota exceeded",
            "quota_timeout": "quota exceeded",
            "too_large": "exceeds server quota",
        }.get(reason)
        if reason_label:
            text = f'<span foreground="{DC_ORANGE_700}">Failed ({reason_label})</span>'
        else:
            text = f'<span foreground="{DC_ORANGE_700}">Failed</span>'
    elif item["direction"] == "received":
        # Sky blue — completed incoming transfer.
        text = f'<span foreground="{DC_BLUE_400}">Received</span>'
    elif delivered:
        # Brand success — green is retired.
        text = f'<span foreground="{DC_BLUE_500}">Delivered</span>'
    elif recv_dl > 0 and recv_total > 0:
        text = f"Delivering {recv_dl}/{recv_total}"
    else:
        text = "Sent"

    # Progress bar state: (show, css_class, fraction)
    if item_status == TransferStatus.WAITING or chunks_dl < 0:
        # No progress bar for waiting — the yellow text is the
        # whole signal. (Pulse + fraction=0 would imply motion
        # where there is none.) Same legacy-row fallback as
        # above so stale chunks_downloaded=-1 rows don't paint
        # a negative fraction.
        bar = (False, None, 0.0)
    elif item_status == TransferStatus.WAITING_STREAM:
        # Same reasoning as classic WAITING: yellow text is the
        # signal, no bar. The X→Y counters already convey the
        # in-flight position; a pulse would imply upload motion
        # that is precisely what's stalled.
        bar = (False, None, 0.0)
    elif item_status == TransferStatus.ABORTED:
        # Terminal — nothing to show motion for.
        bar = (False, None, 0.0)
    elif item_status == TransferStatus.UPLOADING and chunks_total > 0:
        bar = (True, "upload-bar", chunks_dl / chunks_total)
    elif item_status == TransferStatus.SENDING and stream_total > 0:
        # Streaming phases (matches the label branches above):
        #   recv_dl == 0               → upload fraction, yellow
        #                                (visually the same as
        #                                classic UPLOADING).
        #   recv_dl > 0, upload in
        #                 progress     → delivery fraction, blue
        #                                (blue denotes real
        #                                overlap).
        #   recv_dl > 0, upload done   → delivery fraction, blue
        #                                (same blue; label flips
        #                                to "Delivering").
        if recv_dl == 0:
            bar = (True, "upload-bar", chunks_up / stream_total)
        else:
            bar = (True, "delivery-bar", recv_dl / stream_total)
    elif item_status == TransferStatus.DOWNLOADING and chunks_total > 0:
        bar = (True, "download-bar", chunks_dl / chunks_total)
    elif (item["direction"] == "sent" and not delivered
            and item_status == TransferStatus.COMPLETE):
        if recv_dl > 0 and recv_total > 0:
            bar = (True, "delivery-bar", recv_dl / recv_total)
        else:
            bar = (True, "delivery-bar pulse-bar", 1.0)
    else:
        bar = (False, None, 0.0)

    return text, bar


def row_key(item: dict):
    """Unique row identity for history-window diffing.
    Transfer-pipeline items have a real `transfer_id`; fn-payload
    items (clipboard text/image, .fn.unpair) go through fasttrack
    and persist with an EMPTY-STRING transfer_id, which collides on
    `dict.get(...)` lookups (empty-string is a present value, not
    a default-trigger). Fall back to a (timestamp, filename, label
    prefix) composite that's unique-enough across realistic clipboard
    cadences."""
    tid = item.get("transfer_id")
    if tid:
        return tid
    return (
        item.get("timestamp", 0),
        item.get("filename", ""),
        (item.get("display_label", "") or "")[:40],
    )
