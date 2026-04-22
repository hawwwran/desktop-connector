package com.desktopconnector.network

import com.desktopconnector.data.TransferStatus
import kotlinx.coroutines.delay

/**
 * Terminal outcome of the streaming-upload state machine.
 *
 * `uploadStreamLoop` is a pure function of typed [ApiClient.ChunkUploadResult]
 * values and an injectable clock/sleep pair. It drives chunks through the
 * server sequentially and returns one of these when the loop ends — the
 * Worker layer above maps each outcome into the appropriate Room status
 * + `Result.*` for WorkManager.
 *
 * Keeping this function off [UploadWorker] means we can JVM-unit-test
 * the full state machine (happy path, 507 recovery, 507 → 30-min
 * timeout, 410 abort, network exhaustion) with no phone, no emulator,
 * no WorkManager. See UploadStreamLoopTest.
 */
internal sealed class StreamOutcome {
    /** Every chunk Ok'd. Worker flips row to COMPLETE. */
    object Delivered : StreamOutcome()

    /** Recipient DELETE'd mid-stream (or sender saw 410 on a chunk).
     *  Worker marks the row ABORTED with this [reason]. */
    data class AbortedByRecipient(val reason: String?) : StreamOutcome()

    /** Sender gave up — either the quota backpressure window
     *  (`"quota_timeout"`) or the continuous-network-error window
     *  (`"network"`). Worker marks the row FAILED with this reason. */
    data class SenderFailed(val reason: String) : StreamOutcome()

    /** 401/403 observed on a chunk upload. ApiClient already emitted
     *  the AuthObservation via the shared flow; Worker bails out of
     *  the transfer and lets ConnectionManager latch the banner. */
    object AuthLost : StreamOutcome()
}

/**
 * Sender streaming state machine (D.4a).
 *
 * Walks chunks through [uploadChunk] one at a time. Branches on the
 * typed [ApiClient.ChunkUploadResult]:
 *
 *   - Ok              → report progress, reset both retry budgets,
 *                       move to next chunk. If we were in a WAITING
 *                       state, clear it.
 *   - StorageFull     → enter WAITING state (stamped with the first
 *                       507 timestamp), exponential-backoff the sleep
 *                       2→4→8→16→cap 30 s, keep retrying the SAME
 *                       chunk until either it succeeds or the 30 min
 *                       total window expires.
 *   - Aborted         → terminal; return AbortedByRecipient.
 *   - NetworkError /
 *     ServerError     → count elapsed time since first failure; fixed
 *                       5 s cadence (matching classic); once the
 *                       120 s window expires return SenderFailed("network").
 *   - AuthError       → terminal; return AuthLost. Caller lets the
 *                       banner latch kick in and may Result.retry()
 *                       at the Worker layer.
 *
 * Clock + sleep are injected so unit tests can advance virtual time
 * without blocking.  Tests also pass a fake [uploadChunk] that scripts
 * per-index outcomes.
 *
 * Not thread-safe — intended to run on a single coroutine at a time.
 */
/**
 * D.4b helper: given the current sender-side row status and the
 * tracker-observed `deliveryChunks`, returns the status to flip TO, or
 * null if no flip is needed. Idempotent.
 *
 * Transition rules (streaming only; called from the streaming branch):
 *   UPLOADING      → SENDING      when deliveryChunks > 0 (recipient
 *                                  ack'd at least one chunk — the
 *                                  classic "upload then deliver" phases
 *                                  have merged).
 *   WAITING_STREAM → SENDING      when exiting 507 backoff AND the
 *                                  recipient has drained at least one
 *                                  chunk.
 *   WAITING_STREAM → UPLOADING    when exiting 507 backoff AND the
 *                                  recipient hasn't drained anything
 *                                  yet. Classic-shaped flow resumes.
 *   SENDING        → SENDING      idempotent (returns null).
 *   * → *                         no-op for any other combination
 *                                  (FAILED, ABORTED, COMPLETE, DELIVERED,
 *                                  DELIVERING are all invalid inputs to
 *                                  the streaming sender state machine).
 *
 * Field-ownership: only the upload loop writes `status`. The tracker
 * writes `deliveryChunks`; we only read it here.
 */
internal fun streamingSenderStatusTarget(
    currentStatus: TransferStatus,
    deliveryChunks: Int,
    isExitingWaitingStream: Boolean = false,
): TransferStatus? {
    return when {
        currentStatus == TransferStatus.SENDING -> null
        isExitingWaitingStream -> if (deliveryChunks > 0) TransferStatus.SENDING else TransferStatus.UPLOADING
        currentStatus == TransferStatus.UPLOADING && deliveryChunks > 0 -> TransferStatus.SENDING
        currentStatus == TransferStatus.WAITING_STREAM && deliveryChunks > 0 -> TransferStatus.SENDING
        else -> null
    }
}

internal suspend fun uploadStreamLoop(
    chunkCount: Int,
    uploadChunk: suspend (chunkIndex: Int) -> ApiClient.ChunkUploadResult,
    onChunkOk: suspend (chunkIndex: Int) -> Unit,
    onEnterWaitingStream: suspend (startedAtMs: Long) -> Unit,
    onExitWaitingStream: suspend () -> Unit,
    clock: () -> Long = System::currentTimeMillis,
    sleep: suspend (Long) -> Unit = { delay(it) },
    waitingStreamMaxWindowMs: Long = 30L * 60 * 1000,
    networkErrorBudgetMs: Long = 120_000,
    initialWaitingBackoffMs: Long = 2_000,
    maxWaitingBackoffMs: Long = 30_000,
    networkRetryCadenceMs: Long = 5_000,
): StreamOutcome {
    if (chunkCount <= 0) return StreamOutcome.Delivered

    var i = 0
    var waitingStartedAtMs: Long? = null
    var waitingBackoffMs = initialWaitingBackoffMs
    var networkErrorStartedAtMs: Long? = null

    while (i < chunkCount) {
        val result = uploadChunk(i)
        when (result) {
            is ApiClient.ChunkUploadResult.Ok -> {
                onChunkOk(i)
                // If we were waiting on 507 backpressure, the row's in
                // WAITING_STREAM — tell the caller to flip back to the
                // normal upload status. Idempotent; safe to call even
                // on the first Ok.
                if (waitingStartedAtMs != null) {
                    onExitWaitingStream()
                    waitingStartedAtMs = null
                    waitingBackoffMs = initialWaitingBackoffMs
                }
                networkErrorStartedAtMs = null
                i++
            }
            is ApiClient.ChunkUploadResult.StorageFull -> {
                val now = clock()
                if (waitingStartedAtMs == null) {
                    // First 507 — enter WAITING_STREAM. Caller stamps
                    // the row's `waitingStartedAt` for the D.5 scrub.
                    waitingStartedAtMs = now
                    onEnterWaitingStream(now)
                }
                val elapsed = now - waitingStartedAtMs!!
                if (elapsed >= waitingStreamMaxWindowMs) {
                    return StreamOutcome.SenderFailed("quota_timeout")
                }
                sleep(waitingBackoffMs)
                waitingBackoffMs = (waitingBackoffMs * 2).coerceAtMost(maxWaitingBackoffMs)
                // Same chunk index — keep retrying the 507.
            }
            is ApiClient.ChunkUploadResult.Aborted -> {
                return StreamOutcome.AbortedByRecipient(result.reason)
            }
            is ApiClient.ChunkUploadResult.NetworkError,
            is ApiClient.ChunkUploadResult.ServerError -> {
                val now = clock()
                if (networkErrorStartedAtMs == null) {
                    networkErrorStartedAtMs = now
                }
                val elapsed = now - networkErrorStartedAtMs!!
                if (elapsed >= networkErrorBudgetMs) {
                    return StreamOutcome.SenderFailed("network")
                }
                sleep(networkRetryCadenceMs)
                // Don't increment i — retry the same chunk. Don't
                // reset networkErrorStartedAtMs — budget is continuous
                // since the FIRST failure, not since the latest.
            }
            is ApiClient.ChunkUploadResult.AuthError -> {
                return StreamOutcome.AuthLost
            }
        }
    }
    return StreamOutcome.Delivered
}
