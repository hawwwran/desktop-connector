package com.desktopconnector.service

import com.desktopconnector.data.TransferStatus
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * JVM unit tests for the two pure decision helpers backing the
 * delivery-tracker absent-row stall (Fix A) and the startup orphan
 * sweep (Fix B). See `docs/plans/android-radio-tail-cost.md` round
 * bf83c67/_9.txt for the radio-cost evidence that motivated both.
 */
class DeliveryTrackerDecisionsTest {

    private val stallTimeoutMs = 2L * 60 * 1000  // 2 min, matches DELIVERY_STALL_TIMEOUT_MS

    // -----------------------------------------------------------------
    // Fix A: trackerAbsentDecision
    // -----------------------------------------------------------------

    @Test fun `absent for the first time starts the clock`() {
        val decision = trackerAbsentDecision(
            prevTimestampMs = null,
            nowMs = 10_000,
            stallTimeoutMs = stallTimeoutMs,
        )
        assertEquals(TrackerAbsentDecision.StartClock, decision)
    }

    @Test fun `absent within stall window keeps waiting`() {
        val decision = trackerAbsentDecision(
            prevTimestampMs = 10_000,
            nowMs = 10_000 + 30_000,  // 30 s elapsed, under 2 min
            stallTimeoutMs = stallTimeoutMs,
        )
        assertEquals(TrackerAbsentDecision.KeepWaiting, decision)
    }

    @Test fun `absent right at the boundary keeps waiting`() {
        // Strictly greater than stallTimeoutMs is the give-up trigger;
        // exact equality is still inside the window.
        val decision = trackerAbsentDecision(
            prevTimestampMs = 10_000,
            nowMs = 10_000 + stallTimeoutMs,
            stallTimeoutMs = stallTimeoutMs,
        )
        assertEquals(TrackerAbsentDecision.KeepWaiting, decision)
    }

    @Test fun `absent past stall window gives up`() {
        val decision = trackerAbsentDecision(
            prevTimestampMs = 10_000,
            nowMs = 10_000 + stallTimeoutMs + 1,
            stallTimeoutMs = stallTimeoutMs,
        )
        assertEquals(TrackerAbsentDecision.GiveUp, decision)
    }

    @Test fun `absent for much longer than stall window still gives up exactly once`() {
        // Caller is expected to remove the tid from trackerAbsentSince
        // on GiveUp so a re-appearance starts a fresh clock; the helper
        // itself doesn't memoize.
        val decision = trackerAbsentDecision(
            prevTimestampMs = 10_000,
            nowMs = 10_000 + 24L * 60 * 60 * 1000,  // 24 h
            stallTimeoutMs = stallTimeoutMs,
        )
        assertEquals(TrackerAbsentDecision.GiveUp, decision)
    }

    // -----------------------------------------------------------------
    // Fix B: orphanSweepAction
    // -----------------------------------------------------------------

    @Test fun `present in sent-status leaves the row alone regardless of status`() {
        // Even rows in UPLOADING / WAITING_STREAM that the tracker
        // would consider mid-flight stay LEAVE if the server still has
        // them — the regular tracker handles in-flight progress.
        val statuses = listOf(
            TransferStatus.UPLOADING,
            TransferStatus.WAITING_STREAM,
            TransferStatus.SENDING,
            TransferStatus.COMPLETE,
        )
        for (status in statuses) {
            assertEquals(
                "presence wins for $status",
                OrphanSweepAction.LEAVE,
                orphanSweepAction(status, presentInSentStatus = true),
            )
        }
    }

    @Test fun `absent COMPLETE row is marked delivered`() {
        // Classic upload finished; server forgot the row (pruned out
        // of LIMIT-50 or already past 7d TRANSFER_EXPIRY). Most likely
        // delivered before being forgotten.
        assertEquals(
            OrphanSweepAction.MARK_DELIVERED,
            orphanSweepAction(TransferStatus.COMPLETE, presentInSentStatus = false),
        )
    }

    @Test fun `absent SENDING row is marked delivered`() {
        // Streaming upload finished (no intermediate COMPLETE for
        // streaming — sender row stays SENDING until delivered=1).
        // Server forgot it; same reasoning as COMPLETE.
        assertEquals(
            OrphanSweepAction.MARK_DELIVERED,
            orphanSweepAction(TransferStatus.SENDING, presentInSentStatus = false),
        )
    }

    @Test fun `absent UPLOADING streaming row is marked aborted`() {
        // Streaming upload never finished; server's 24h INCOMPLETE_EXPIRY
        // pruned the incomplete row. We can't claim delivery — flip
        // terminal as aborted so the row stops being polled.
        assertEquals(
            OrphanSweepAction.MARK_ABORTED,
            orphanSweepAction(TransferStatus.UPLOADING, presentInSentStatus = false),
        )
    }

    @Test fun `absent WAITING_STREAM row is marked aborted`() {
        // Stuck in 507 backoff when the session ended; never reached
        // SENDING. Same handling as UPLOADING.
        assertEquals(
            OrphanSweepAction.MARK_ABORTED,
            orphanSweepAction(TransferStatus.WAITING_STREAM, presentInSentStatus = false),
        )
    }

    @Test fun `absent rows with terminal local status are left alone`() {
        // The DAO query already filters these out, but the helper
        // shouldn't fire MARK_* if a schema change ever loosens that.
        val terminal = listOf(
            TransferStatus.QUEUED,
            TransferStatus.PREPARING,
            TransferStatus.WAITING,
            TransferStatus.FAILED,
            TransferStatus.ABORTED,
            TransferStatus.DELIVERING,
        )
        for (status in terminal) {
            assertEquals(
                "defensive LEAVE for unexpected $status",
                OrphanSweepAction.LEAVE,
                orphanSweepAction(status, presentInSentStatus = false),
            )
        }
    }
}
