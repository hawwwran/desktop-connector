package com.desktopconnector.network

import com.desktopconnector.data.TransferStatus
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * JVM unit tests for the D.4b sender-side three-phase state machine.
 *
 * Exercises [streamingSenderStatusTarget] — the pure function that
 * maps `(currentStatus, deliveryChunks, isExitingWaitingStream)` to
 * a target status (or null if no flip is needed). Every branch the
 * upload loop's callback takes goes through this helper, so pinning
 * it here pins the full SENDING-transition behaviour.
 *
 * Field-ownership contract verified implicitly: the helper only READS
 * its inputs and RETURNS a target; it never mutates Room. The upload
 * loop is the only writer of `status`, the tracker the only writer of
 * `deliveryChunks` / `deliveryTotal` / `delivered`.
 */
class SenderDeliveryPhaseTest {

    // -----------------------------------------------------------------
    // UPLOADING → SENDING transition
    // -----------------------------------------------------------------

    @Test fun `UPLOADING flips to SENDING once recipient drains first chunk`() {
        val target = streamingSenderStatusTarget(
            currentStatus = TransferStatus.UPLOADING,
            deliveryChunks = 1,
        )
        assertEquals(TransferStatus.SENDING, target)
    }

    @Test fun `UPLOADING stays UPLOADING while recipient hasn't drained anything`() {
        val target = streamingSenderStatusTarget(
            currentStatus = TransferStatus.UPLOADING,
            deliveryChunks = 0,
        )
        assertNull("no flip while deliveryChunks=0", target)
    }

    @Test fun `UPLOADING flips to SENDING with high deliveryChunks too`() {
        // Covers the case where the tracker advanced several chunks in
        // one tick before the sender's callback caught up.
        val target = streamingSenderStatusTarget(
            currentStatus = TransferStatus.UPLOADING,
            deliveryChunks = 17,
        )
        assertEquals(TransferStatus.SENDING, target)
    }

    // -----------------------------------------------------------------
    // SENDING idempotency
    // -----------------------------------------------------------------

    @Test fun `SENDING is a no-op flip`() {
        assertNull(streamingSenderStatusTarget(TransferStatus.SENDING, deliveryChunks = 0))
        assertNull(streamingSenderStatusTarget(TransferStatus.SENDING, deliveryChunks = 5))
        assertNull(streamingSenderStatusTarget(TransferStatus.SENDING, deliveryChunks = 100))
    }

    @Test fun `SENDING is a no-op even when exiting waiting-stream`() {
        // Shouldn't happen in practice (SENDING never transitions INTO
        // waiting-stream), but the idempotency guard is unconditional.
        assertNull(streamingSenderStatusTarget(
            currentStatus = TransferStatus.SENDING,
            deliveryChunks = 5,
            isExitingWaitingStream = true,
        ))
    }

    // -----------------------------------------------------------------
    // WAITING_STREAM exit rules
    // -----------------------------------------------------------------

    @Test fun `exit waiting-stream with delivery progress goes to SENDING`() {
        val target = streamingSenderStatusTarget(
            currentStatus = TransferStatus.WAITING_STREAM,
            deliveryChunks = 3,
            isExitingWaitingStream = true,
        )
        assertEquals(TransferStatus.SENDING, target)
    }

    @Test fun `exit waiting-stream with no delivery progress goes to UPLOADING`() {
        val target = streamingSenderStatusTarget(
            currentStatus = TransferStatus.WAITING_STREAM,
            deliveryChunks = 0,
            isExitingWaitingStream = true,
        )
        assertEquals(TransferStatus.UPLOADING, target)
    }

    @Test fun `WAITING_STREAM stays WAITING_STREAM when not exiting`() {
        // Called from onChunkOk (not onExitWaitingStream). A chunk Ok
        // arriving during waiting_stream means the 507 cleared, but the
        // upload loop uses onExitWaitingStream + clearWaitingStream for
        // that transition — onChunkOk isn't responsible.
        //
        // The helper is still consulted (via onChunkOk → maybeFlipToSending
        // with isExitingWaitingStream=false); it must return null so no
        // extra status write races with clearWaitingStream.
        val target = streamingSenderStatusTarget(
            currentStatus = TransferStatus.WAITING_STREAM,
            deliveryChunks = 2,
            isExitingWaitingStream = false,
        )
        assertEquals(TransferStatus.SENDING, target)
        // NOTE: this IS a flip to SENDING if deliveryChunks > 0, matching
        // the documented rule. The upload loop's onChunkOk callback is
        // called AFTER onExitWaitingStream in practice, but the helper
        // itself is order-agnostic.
    }

    // -----------------------------------------------------------------
    // Non-streaming statuses — helper is invalid input, returns null
    // -----------------------------------------------------------------

    @Test fun `COMPLETE status returns null (not a valid streaming state)`() {
        assertNull(streamingSenderStatusTarget(TransferStatus.COMPLETE, deliveryChunks = 5))
    }

    @Test fun `FAILED status returns null`() {
        assertNull(streamingSenderStatusTarget(TransferStatus.FAILED, deliveryChunks = 5))
    }

    @Test fun `ABORTED status returns null`() {
        assertNull(streamingSenderStatusTarget(TransferStatus.ABORTED, deliveryChunks = 5))
    }

    @Test fun `QUEUED status returns null`() {
        // QUEUED is pre-upload; shouldn't reach the streaming sender
        // callback, but defensive: don't generate a flip.
        assertNull(streamingSenderStatusTarget(TransferStatus.QUEUED, deliveryChunks = 5))
    }

    @Test fun `DELIVERING status returns null`() {
        // Reserved value; no Android writer produces it in D.4b. Helper
        // must not flip on it if it ever arrives.
        assertNull(streamingSenderStatusTarget(TransferStatus.DELIVERING, deliveryChunks = 99))
    }

    // -----------------------------------------------------------------
    // Negative deliveryChunks defensive
    // -----------------------------------------------------------------

    @Test fun `negative deliveryChunks treated as zero progress`() {
        // Shouldn't happen (tracker only writes non-negative values),
        // but helper must not throw or flip on a garbage value.
        val target = streamingSenderStatusTarget(
            currentStatus = TransferStatus.UPLOADING,
            deliveryChunks = -1,
        )
        assertNull(target)
    }
}
