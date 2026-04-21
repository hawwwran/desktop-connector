"""Pin the streaming-aware history row shape on the desktop client.

C.2 deliverable: the history layer gains optional streaming fields
(``mode``, ``chunks_uploaded``, ``abort_reason``), the
``TransferStatus`` constant namespace, and a tightened
``get_undelivered_transfer_ids`` that excludes ``aborted`` rows from
the delivery-tracker sweep.

Nothing in this file exercises the server. We're pinning the Python
data model so C.3 (recipient receive loop) and C.4 (sender state
machine) can write these fields without a follow-up schema scuffle.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

sys.path.insert(0, REPO_ROOT)

from desktop.src.history import TransferHistory, TransferStatus  # noqa: E402


class TransferStatusConstantsTests(unittest.TestCase):
    """The canonical status strings are the wire contract between the
    sender writers, receiver writers, and the history renderer. Pin
    them so typos can't drift any of the three out of sync."""

    def test_all_streaming_statuses_exposed(self):
        self.assertEqual(TransferStatus.SENDING, "sending")
        self.assertEqual(TransferStatus.WAITING_STREAM, "waiting_stream")
        self.assertEqual(TransferStatus.ABORTED, "aborted")

    def test_classic_statuses_preserved(self):
        # These values are persisted in existing users' history.json
        # files — changing any of them is a data migration.
        self.assertEqual(TransferStatus.UPLOADING, "uploading")
        self.assertEqual(TransferStatus.WAITING, "waiting")
        self.assertEqual(TransferStatus.COMPLETE, "complete")
        self.assertEqual(TransferStatus.DOWNLOADING, "downloading")
        self.assertEqual(TransferStatus.FAILED, "failed")


class HistoryStreamingRowShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-history-"))
        self.history = TransferHistory(self.tmp)

    def test_classic_row_defaults_mode_and_chunks_uploaded(self):
        """Old callers that pass no streaming kwargs still get a row
        that the streaming-aware renderer can read cleanly."""
        self.history.add(
            filename="f.bin", display_label="f.bin", direction="sent",
            size=10, transfer_id="tid-classic",
            status=TransferStatus.UPLOADING,
        )
        [row] = self.history.items
        self.assertEqual(row["mode"], "classic")
        self.assertEqual(row["chunks_uploaded"], 0)
        self.assertNotIn("abort_reason", row)

    def test_streaming_row_persists_mode_and_chunks_uploaded(self):
        self.history.add(
            filename="g.bin", display_label="g.bin", direction="sent",
            size=20, transfer_id="tid-streaming",
            status=TransferStatus.SENDING,
            chunks_total=5,
            mode="streaming", chunks_uploaded=3,
        )
        [row] = self.history.items
        self.assertEqual(row["mode"], "streaming")
        self.assertEqual(row["chunks_uploaded"], 3)

    def test_aborted_row_carries_reason(self):
        self.history.add(
            filename="h.bin", display_label="h.bin", direction="sent",
            size=30, transfer_id="tid-aborted",
            status=TransferStatus.ABORTED,
            mode="streaming",
            abort_reason="recipient_abort",
        )
        [row] = self.history.items
        self.assertEqual(row["status"], TransferStatus.ABORTED)
        self.assertEqual(row["abort_reason"], "recipient_abort")


class UndeliveredTrackingTests(unittest.TestCase):
    """The delivery tracker polls only live outgoing transfers. Rows
    that are terminal (``failed``, ``aborted``) must be excluded so
    the tracker doesn't hammer /sent-status for a server row that
    either never existed or has already been wiped."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-history-undelivered-"))
        self.history = TransferHistory(self.tmp)

    def _seed(self, tid: str, status: str, **extra) -> None:
        self.history.add(
            filename=tid, display_label=tid, direction="sent",
            size=1, transfer_id=tid, status=status, **extra,
        )

    def test_sending_row_is_tracked(self):
        self._seed("tid-sending", TransferStatus.SENDING)
        self.assertIn("tid-sending", self.history.get_undelivered_transfer_ids())

    def test_waiting_stream_row_is_tracked(self):
        self._seed("tid-waiting-stream", TransferStatus.WAITING_STREAM)
        self.assertIn(
            "tid-waiting-stream",
            self.history.get_undelivered_transfer_ids(),
        )

    def test_aborted_row_is_excluded(self):
        self._seed("tid-aborted", TransferStatus.ABORTED,
                   abort_reason="sender_abort")
        self.assertNotIn(
            "tid-aborted",
            self.history.get_undelivered_transfer_ids(),
        )

    def test_failed_row_is_still_excluded(self):
        """Regression: don't accidentally drop failed from the skip set
        while rewriting the predicate."""
        self._seed("tid-failed", TransferStatus.FAILED)
        self.assertNotIn(
            "tid-failed",
            self.history.get_undelivered_transfer_ids(),
        )

    def test_delivered_row_is_excluded(self):
        self._seed("tid-delivered", TransferStatus.COMPLETE)
        self.history.mark_delivered("tid-delivered")
        self.assertNotIn(
            "tid-delivered",
            self.history.get_undelivered_transfer_ids(),
        )

    def test_received_row_is_excluded(self):
        """Recipient rows never drive the sender's delivery tracker."""
        self.history.add(
            filename="in.bin", display_label="in.bin",
            direction="received", size=1,
            transfer_id="tid-recv", status=TransferStatus.COMPLETE,
        )
        self.assertNotIn(
            "tid-recv",
            self.history.get_undelivered_transfer_ids(),
        )


if __name__ == "__main__":
    unittest.main()
