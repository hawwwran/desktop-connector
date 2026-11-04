"""Zombie-WAITING-row scrubber for the History window.

Called at window open AND on every ``build_list`` tick (1-3 s) so
rows age from Waiting to Failed without the user needing to close +
reopen — see CLAUDE.md "WAITING + zombie scrub" for the cross-runtime
contract.
"""

from __future__ import annotations

import time

from ..api_client import STORAGE_FULL_MAX_WINDOW_S
from ..history import TransferStatus
from .context import HistoryContext


def scrub_zombie_waiting(ctx: HistoryContext) -> None:
    """Flip any orphaned waiting row to 'failed' with
    failure_reason='quota_timeout'. A row is orphaned if it's
    been in waiting state longer than STORAGE_FULL_MAX_WINDOW_S
    (30 min) — beyond the retry budget of any still-live send
    subprocess. Called at window open AND on every build_list
    tick so rows age from Waiting → Failed without the user
    needing to close + reopen.

    Covers both waiting flavours:
      * ``waiting`` — classic 507 at init (row never uploaded
        anything; the legacy chunks_downloaded=-1 sentinel also
        qualifies, for back-compat with tray clipboard + --send
        CLI rows written by older builds).
      * ``waiting_stream`` — streaming mid-upload 507 (quota
        back-pressure between sender's write head and recipient's
        drain).
    Both use the same 30-min ceiling — they're the same logical
    budget, just measured from different points in the transfer
    lifecycle.

    Age check prefers waiting_started_at (stamped when the row
    entered waiting), falling back to timestamp. The 30-min window
    is a ceiling, not an instant-kill — a live subprocess must be
    given the full budget before we declare its row dead, otherwise
    the UI flashes Failed while the sender is still retrying and
    eventually succeeds.
    """
    history = ctx.history
    cutoff = int(time.time()) - int(STORAGE_FULL_MAX_WINDOW_S)
    waiting_statuses = {TransferStatus.WAITING, TransferStatus.WAITING_STREAM}
    for it in history.items:
        chunks_dl = it.get("chunks_downloaded", 0) or 0
        is_waiting = (it.get("status") in waiting_statuses
                      or chunks_dl < 0)
        if not is_waiting:
            continue
        age_ref = int(it.get("waiting_started_at") or it.get("timestamp") or 0)
        if age_ref and age_ref < cutoff:
            tid = it.get("transfer_id")
            if tid:
                history.update(tid, status=TransferStatus.FAILED,
                               chunks_downloaded=0, chunks_total=0,
                               failure_reason="quota_timeout")
