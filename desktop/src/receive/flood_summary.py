"""Receive-action plumbing on the Poller side.

``_apply_receive_file_action`` runs the configured action for a saved
file (image-open, document-open, …) and returns whether anything fired
so the caller can suppress a redundant "File received" notification.

``_notify_receive_action_flood_summary`` surfaces the rate-limiter
verdict at the end of a batch so the user knows when actions were
suppressed.

Named ``flood_summary`` rather than ``receive_actions`` because
``desktop/src/receive_actions.py`` (the limiter + classifier module)
already owns that name.
"""

import logging
from pathlib import Path

from ..receive_actions import (
    RECEIVE_KIND_OTHER,
    ReceiveActionBatch,
    ReceiveActionFloodSummary,
    apply_receive_action,
    classify_received_file,
)

log = logging.getLogger(__name__)


class FloodSummaryMixin:
    def _notify_receive_action_flood_summary(
        self,
        summary: ReceiveActionFloodSummary,
    ) -> None:
        if not summary.has_suppressed:
            return

        item_word = "item" if summary.batch_size == 1 else "items"
        action_word = "action" if summary.total_suppressed == 1 else "actions"
        body = (
            f"Received {summary.batch_size} {item_word}. "
            f"Skipped {summary.total_suppressed} automatic {action_word} "
            "to prevent flooding."
        )
        log.info(
            "receive_action.flood_limited batch_size=%d suppressed=%d",
            summary.batch_size,
            summary.total_suppressed,
        )
        try:
            self.platform.notifications.notify("Receive actions limited", body)
        except Exception:
            log.exception("receive_action.flood_notification.failed")

    def _apply_receive_file_action(
        self,
        filepath: Path,
        *,
        receive_action_batch: ReceiveActionBatch | None = None,
    ) -> bool:
        """Run the configured receive action for a saved file.

        Returns True iff a configured action actually fired successfully
        — used by callers to suppress the redundant "File received"
        notification when the user already saw the action effect (image
        viewer launching, document opening, etc.). Files classified as
        ``RECEIVE_KIND_OTHER`` (no configurable action) always return
        False so the notification still fires.
        """
        kind = classify_received_file(filepath)
        if kind == RECEIVE_KIND_OTHER:
            return False
        result = apply_receive_action(
            self.config,
            self.platform,
            kind,
            path=filepath,
            limiter=self._receive_action_limiter,
            batch=receive_action_batch,
        )
        if not result.ok:
            log.warning(
                "receive_action.file.failed kind=%s",
                kind,
            )
        return result.action_ran
