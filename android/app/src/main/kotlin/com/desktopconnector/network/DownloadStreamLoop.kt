package com.desktopconnector.network

import kotlinx.coroutines.delay

/**
 * Terminal outcome of the streaming-download state machine.
 *
 * `downloadStreamLoop` is a pure function of typed
 * [ApiClient.ChunkDownloadResult] values, injected clock/sleep,
 * and callbacks for decrypt + file write + ack. It drives chunks
 * from the server into plaintext and returns one of these when the
 * loop ends — the PollService layer above maps each outcome into
 * the appropriate Room status and terminal action (wipe `.part`,
 * finalize rename, mark the row ABORTED/FAILED, etc.).
 *
 * Keeping this function off [com.desktopconnector.service.PollService]
 * means we can JVM-unit-test the full recipient state machine
 * (happy path, 425 budget, 410 abort, 3-error network exhaustion,
 * auth loss, decrypt-loop recovery) without an Android runtime,
 * WiFi lock, wake lock, or a real `FileOutputStream`. See
 * ReceiveStreamingTransferTest.
 */
internal sealed class StreamReceiveOutcome {
    /** All chunks successfully fetched, decrypted, written, and acked. */
    object Complete : StreamReceiveOutcome()

    /** Upstream (sender OR server via 410 Gone) aborted mid-stream.
     *  Caller wipes `.part`, marks the row ABORTED with this reason. */
    data class AbortedByUpstream(val reason: String?) : StreamReceiveOutcome()

    /** Recipient-side give-up: stall_timeout (5 min of TooEarly),
     *  network (exhausted 3-attempt budget on consecutive network
     *  errors), or recipient_abort (row deleted mid-loop). Caller
     *  fires DELETE .../{tid} with reason=recipient_abort, marks the
     *  row FAILED with `failureReason=<this>`. */
    data class RecipientAborted(val reason: String) : StreamReceiveOutcome()

    /** 401/403 observed on a chunk GET. ApiClient already emitted
     *  the AuthObservation; caller bails out of the receive and
     *  lets ConnectionManager latch the banner. */
    object AuthLost : StreamReceiveOutcome()
}

/**
 * Recipient streaming state machine (D.3 / D.6b).
 *
 * Walks chunks through [downloadChunk] one at a time and branches
 * on the typed [ApiClient.ChunkDownloadResult]:
 *
 *   - Ok              → [decrypt] → [writeChunk] → [ackChunk] →
 *                       [onProgress]. Reset both retry budgets.
 *                       Move to next chunk.
 *   - TooEarly        → sleep per server's hint clamped to our ramp
 *                       (1 s → 2 s → 4 s → 8 s → cap 10 s). Loop
 *                       back to the same chunk. No progress means
 *                       we drain the 5 min no-progress budget.
 *   - Aborted         → terminal AbortedByUpstream(reason).
 *   - NetworkError /
 *     ServerError     → increment consecutive-error counter; 3 in a
 *                       row → RecipientAborted("network"). Between
 *                       errors, sleep `networkRetryDelayMs(attempt)`.
 *                       Counter resets on the next Ok.
 *   - AuthError       → terminal AuthLost.
 *
 * Plus two orthogonal bail conditions, checked before each chunk
 * attempt:
 *   - `isCancelled()` true  → RecipientAborted("recipient_abort").
 *     Caller user swipe-deleted the Room row.
 *   - [clock] - lastProgress > noProgressBudgetMs →
 *     RecipientAborted("stall_timeout"). Continuous TooEarly for
 *     5 min with no Ok in between.
 *
 * Decrypt failure (AES-GCM InvalidTag; the [decrypt] callback
 * returns null) is treated as "re-fetch this chunk" — a belt-and-
 * suspenders guard against a server-side torn read during
 * concurrent upload. The function sleeps `decryptRetryDelayMs`
 * and loops back. No dedicated retry counter — the no-progress
 * budget catches a truly stuck decrypt (since decrypt retries
 * don't advance lastProgress).
 *
 * Clock + sleep are injected so unit tests can advance virtual
 * time without blocking. Tests script [downloadChunk] / [decrypt] /
 * [ackChunk] / [isCancelled] and assert on the returned outcome.
 *
 * Not thread-safe — intended to run on a single coroutine.
 */
internal suspend fun downloadStreamLoop(
    chunkCount: Int,
    downloadChunk: suspend (chunkIndex: Int) -> ApiClient.ChunkDownloadResult,
    decrypt: (bytes: ByteArray) -> ByteArray?,
    writeChunk: suspend (chunkIndex: Int, plaintext: ByteArray) -> Unit,
    ackChunk: suspend (chunkIndex: Int) -> Boolean,
    onProgress: suspend (chunkIndex: Int) -> Unit,
    isCancelled: suspend () -> Boolean = { false },
    clock: () -> Long = System::currentTimeMillis,
    sleep: suspend (Long) -> Unit = { delay(it) },
    noProgressBudgetMs: Long = 5L * 60 * 1000,
    maxNetworkRetries: Int = 3,
    initialTooEarlyBackoffMs: Long = 1_000,
    maxTooEarlyBackoffMs: Long = 10_000,
    decryptRetryDelayMs: Long = 2_000,
    networkRetryDelayMs: (attempt: Int) -> Long = { it * 2_000L },
): StreamReceiveOutcome {
    if (chunkCount <= 0) return StreamReceiveOutcome.Complete

    var i = 0
    var lastProgressAt = clock()
    var networkRetries = 0
    var tooEarlyBackoffMs = initialTooEarlyBackoffMs

    while (i < chunkCount) {
        if (isCancelled()) {
            return StreamReceiveOutcome.RecipientAborted("recipient_abort")
        }
        val now = clock()
        if (now - lastProgressAt > noProgressBudgetMs) {
            return StreamReceiveOutcome.RecipientAborted("stall_timeout")
        }

        val result = downloadChunk(i)
        when (result) {
            is ApiClient.ChunkDownloadResult.Ok -> {
                val plaintext = decrypt(result.bytes)
                if (plaintext == null) {
                    // Decrypt failure. Belt-and-suspenders retry: the
                    // server's atomic rename on chunk writes *should*
                    // prevent torn reads, but if we observed one we
                    // re-fetch the same index. No dedicated retry
                    // counter — the no-progress budget catches a
                    // genuinely stuck decrypt (lastProgress isn't
                    // updated on this branch).
                    sleep(decryptRetryDelayMs)
                    continue
                }
                writeChunk(i, plaintext)
                // ack_chunk failure is non-fatal — the chunk is
                // already on disk, the server keeps the blob around
                // but will expire it via its own GC. Caller logs.
                ackChunk(i)
                onProgress(i)
                lastProgressAt = clock()
                networkRetries = 0
                tooEarlyBackoffMs = initialTooEarlyBackoffMs
                i++
            }
            is ApiClient.ChunkDownloadResult.TooEarly -> {
                // Use the larger of server hint and our own ramp,
                // clamped to the ceiling. This prevents a runaway
                // hint from pinning us to a multi-minute sleep and
                // prevents our ramp from shipping a too-fast retry
                // when the server explicitly asked for more.
                val serverHint = result.retryAfterMs.coerceAtMost(maxTooEarlyBackoffMs)
                val ownRamp = tooEarlyBackoffMs.coerceAtMost(maxTooEarlyBackoffMs)
                val sleepMs = maxOf(serverHint, ownRamp)
                sleep(sleepMs)
                tooEarlyBackoffMs = (tooEarlyBackoffMs * 2).coerceAtMost(maxTooEarlyBackoffMs)
                // No reset of networkRetries — 425 isn't a network
                // error, but we also don't want to count it AGAINST
                // the network budget.
                // No lastProgressAt update — continuous TooEarly
                // drains the no-progress budget.
            }
            is ApiClient.ChunkDownloadResult.Aborted -> {
                return StreamReceiveOutcome.AbortedByUpstream(result.reason)
            }
            is ApiClient.ChunkDownloadResult.NetworkError,
            is ApiClient.ChunkDownloadResult.ServerError -> {
                networkRetries++
                if (networkRetries >= maxNetworkRetries) {
                    return StreamReceiveOutcome.RecipientAborted("network")
                }
                sleep(networkRetryDelayMs(networkRetries))
            }
            is ApiClient.ChunkDownloadResult.AuthError -> {
                return StreamReceiveOutcome.AuthLost
            }
        }
    }

    return StreamReceiveOutcome.Complete
}
