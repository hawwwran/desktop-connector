package com.desktopconnector.service

import com.desktopconnector.data.TransferStatus

/**
 * Pure decision helpers for the PollService delivery tracker + the
 * startup orphan sweep. Extracted so the policy is JVM-unit-testable
 * — neither helper touches the service, Room, or the wire.
 */

/**
 * What to do when `/api/transfers/sent-status` returned no row for a
 * locally-active outgoing transfer.
 *
 * Pre-fix the tracker silently `continue`'d past the absent tid, which
 * meant the stall safeguard (line ~1277 in PollService) never engaged
 * for transfers the server had GC'd / pruned out of LIMIT-50. Result:
 * a phantom outgoing row in Room kept `getActiveDeliveryIds` non-empty
 * forever, and every screen-on window paid the radio cost of re-asking
 * the same dead question. See `docs/plans/android-radio-tail-cost.md`
 * round bf83c67/_9.txt.
 *
 * Decision states:
 *   - StartClock — first time we've seen this tid absent. Record the
 *     timestamp; defer the give-up decision.
 *   - KeepWaiting — within the stall window (default 2 min, same value
 *     as the existing present-but-not-advancing stall path).
 *   - GiveUp — exceeded the stall window. Caller adds the tid to
 *     in-memory `trackerGaveUp` (mirrors the classic-mode stall path).
 */
internal sealed class TrackerAbsentDecision {
    object StartClock : TrackerAbsentDecision()
    object KeepWaiting : TrackerAbsentDecision()
    object GiveUp : TrackerAbsentDecision()
}

internal fun trackerAbsentDecision(
    prevTimestampMs: Long?,
    nowMs: Long,
    stallTimeoutMs: Long,
): TrackerAbsentDecision {
    if (prevTimestampMs == null) return TrackerAbsentDecision.StartClock
    return if (nowMs - prevTimestampMs > stallTimeoutMs) {
        TrackerAbsentDecision.GiveUp
    } else {
        TrackerAbsentDecision.KeepWaiting
    }
}

/**
 * What the startup orphan sweep should do with an undelivered outgoing
 * transfer older than the orphan-age threshold.
 *
 * The sweep makes a SINGLE `/sent-status` call at PollService start and
 * decides per-row from the local `status` + whether the server still
 * knows about the transfer:
 *
 *   present in sent-status  →  LEAVE (the regular tracker will handle it)
 *   absent, status reached COMPLETE / SENDING (upload finished)
 *                           →  MARK_DELIVERED (server most likely
 *                              marked-delivered then dropped out of
 *                              LIMIT-50, or the 7-day TRANSFER_EXPIRY
 *                              already pruned it)
 *   absent, status stuck in UPLOADING / WAITING_STREAM (upload never
 *   finished)               →  MARK_ABORTED reason="tracking_expired"
 *                              (server's 24h INCOMPLETE_EXPIRY pruned
 *                              the incomplete row; there's no way it
 *                              completed in the meantime)
 *   any other status        →  LEAVE (defensive; the DAO query already
 *                              filters to the four above, but if the
 *                              schema grows this keeps us safe)
 */
internal enum class OrphanSweepAction { LEAVE, MARK_DELIVERED, MARK_ABORTED }

internal fun orphanSweepAction(
    localStatus: TransferStatus,
    presentInSentStatus: Boolean,
): OrphanSweepAction {
    if (presentInSentStatus) return OrphanSweepAction.LEAVE
    return when (localStatus) {
        TransferStatus.COMPLETE, TransferStatus.SENDING ->
            OrphanSweepAction.MARK_DELIVERED
        TransferStatus.UPLOADING, TransferStatus.WAITING_STREAM ->
            OrphanSweepAction.MARK_ABORTED
        else -> OrphanSweepAction.LEAVE
    }
}
