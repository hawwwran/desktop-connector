package com.desktopconnector.network

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM unit tests for the D.3 / D.6b recipient streaming state machine.
 *
 * Exercises `downloadStreamLoop` against scripted
 * [ApiClient.ChunkDownloadResult] values, fake decrypt / write / ack
 * callbacks, and a virtual clock. No Android runtime, no
 * [com.desktopconnector.service.PollService], no real file IO. The
 * goal is to pin every branch the receiver can take — happy path,
 * TooEarly budget exhaustion, Aborted surfacing, 3-attempt network
 * retry budget, auth lost, decrypt-retry recovery, and the
 * cancellation / no-progress bail conditions.
 */
class ReceiveStreamingTransferTest {

    private class Script(vararg steps: ApiClient.ChunkDownloadResult) {
        private val steps = steps.toMutableList()
        val callsFor: MutableMap<Int, Int> = mutableMapOf()
        fun next(index: Int): ApiClient.ChunkDownloadResult {
            callsFor[index] = (callsFor[index] ?: 0) + 1
            return if (steps.size > 1) steps.removeAt(0) else steps[0]
        }
    }

    private class VirtualClock(startMs: Long = 0L) {
        var nowMs: Long = startMs
        val clock: () -> Long = { nowMs }
        val sleep: suspend (Long) -> Unit = { ms -> nowMs += ms }
    }

    /** Trivial "decrypt": strip the leading byte (which tests use to
     *  flag a pseudo-encrypted blob), return the rest. `null` means
     *  decrypt failure. */
    private fun strippingDecrypt(bytes: ByteArray): ByteArray = bytes.copyOfRange(1, bytes.size)

    /** Fake "encrypted" blob for chunk N — first byte = index+1 so
     *  tests can assert ordering of writes. */
    private fun okChunk(index: Int, payloadSize: Int = 4) =
        ApiClient.ChunkDownloadResult.Ok(
            ByteArray(payloadSize + 1) { i -> if (i == 0) (index + 1).toByte() else (index * 10 + i).toByte() }
        )

    // -----------------------------------------------------------------
    // Happy path
    // -----------------------------------------------------------------

    @Test fun `all chunks Ok returns Complete and writes each plaintext in order`() = runTest {
        val clock = VirtualClock()
        val written = mutableListOf<Pair<Int, ByteArray>>()
        val acked = mutableListOf<Int>()
        val progress = mutableListOf<Int>()

        val outcome = downloadStreamLoop(
            chunkCount = 5,
            downloadChunk = { index -> okChunk(index, payloadSize = 8) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { index, plaintext -> written.add(index to plaintext) },
            ackChunk = { index -> acked.add(index); true },
            onProgress = { index -> progress.add(index) },
            clock = clock.clock,
            sleep = clock.sleep,
        )

        assertTrue("expected Complete, got $outcome", outcome is StreamReceiveOutcome.Complete)
        assertEquals(listOf(0, 1, 2, 3, 4), written.map { it.first })
        assertEquals(listOf(0, 1, 2, 3, 4), acked)
        assertEquals(listOf(0, 1, 2, 3, 4), progress)
    }

    @Test fun `empty chunk count short-circuits to Complete`() = runTest {
        var called = false
        val outcome = downloadStreamLoop(
            chunkCount = 0,
            downloadChunk = { called = true; okChunk(0) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamReceiveOutcome.Complete)
        assertTrue("downloadChunk must not be called for chunkCount=0", !called)
    }

    // -----------------------------------------------------------------
    // TooEarly recovery + budget
    // -----------------------------------------------------------------

    @Test fun `TooEarly then Ok retries same chunk and succeeds`() = runTest {
        val script = Script(
            ApiClient.ChunkDownloadResult.TooEarly(retryAfterMs = 500),
            ApiClient.ChunkDownloadResult.TooEarly(retryAfterMs = 500),
            okChunk(0),
            okChunk(1),
        )
        val clock = VirtualClock()
        val outcome = downloadStreamLoop(
            chunkCount = 2,
            downloadChunk = { script.next(it) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = clock.clock,
            sleep = clock.sleep,
        )
        assertTrue("expected Complete, got $outcome", outcome is StreamReceiveOutcome.Complete)
        // Chunk 0 got 3 GETs (TooEarly, TooEarly, Ok); chunk 1 got 1 GET.
        assertEquals(3, script.callsFor[0])
        assertEquals(1, script.callsFor[1])
    }

    @Test fun `TooEarly backoff walks 1s-2s-4s-8s-10s cap`() = runTest {
        val clock = VirtualClock()
        val sleeps = mutableListOf<Long>()
        val captureSleep: suspend (Long) -> Unit = { ms ->
            sleeps.add(ms); clock.nowMs += ms
        }
        val outcome = downloadStreamLoop(
            chunkCount = 1,
            downloadChunk = { ApiClient.ChunkDownloadResult.TooEarly(retryAfterMs = 0) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = clock.clock,
            sleep = captureSleep,
            noProgressBudgetMs = 90_000,  // 90 s so we capture a few cycles and bail
        )
        assertTrue("eventually gave up", outcome is StreamReceiveOutcome.RecipientAborted)
        assertEquals("stall_timeout", (outcome as StreamReceiveOutcome.RecipientAborted).reason)
        val expectedPrefix = listOf(1_000L, 2_000L, 4_000L, 8_000L, 10_000L)
        assertEquals(expectedPrefix, sleeps.take(5))
        assertTrue("capped at 10_000 after prefix", sleeps.drop(5).all { it == 10_000L })
    }

    @Test fun `TooEarly server hint larger than ramp wins`() = runTest {
        val script = Script(
            ApiClient.ChunkDownloadResult.TooEarly(retryAfterMs = 5_000),
            okChunk(0),
        )
        val clock = VirtualClock()
        val sleeps = mutableListOf<Long>()
        val outcome = downloadStreamLoop(
            chunkCount = 1,
            downloadChunk = { script.next(it) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = clock.clock,
            sleep = { ms -> sleeps.add(ms); clock.nowMs += ms },
        )
        assertTrue(outcome is StreamReceiveOutcome.Complete)
        assertEquals("slept per server hint (5s >> initial ramp 1s)", 5_000L, sleeps[0])
    }

    @Test fun `continuous TooEarly past no-progress budget returns stall_timeout`() = runTest {
        val clock = VirtualClock()
        val outcome = downloadStreamLoop(
            chunkCount = 1,
            downloadChunk = { ApiClient.ChunkDownloadResult.TooEarly(retryAfterMs = 0) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = clock.clock,
            sleep = clock.sleep,
        )
        assertTrue(outcome is StreamReceiveOutcome.RecipientAborted)
        assertEquals("stall_timeout", (outcome as StreamReceiveOutcome.RecipientAborted).reason)
        assertTrue("clock past 5 min", clock.nowMs >= 5 * 60 * 1000)
    }

    // -----------------------------------------------------------------
    // Aborted (410 Gone) surfacing
    // -----------------------------------------------------------------

    @Test fun `410 on chunk 2 returns AbortedByUpstream with reason`() = runTest {
        val script = Script(
            okChunk(0),
            okChunk(1),
            ApiClient.ChunkDownloadResult.Aborted("sender_abort"),
        )
        val written = mutableListOf<Int>()
        val outcome = downloadStreamLoop(
            chunkCount = 5,
            downloadChunk = { script.next(it) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { i, _ -> written.add(i) },
            ackChunk = { true },
            onProgress = { },
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamReceiveOutcome.AbortedByUpstream)
        assertEquals("sender_abort", (outcome as StreamReceiveOutcome.AbortedByUpstream).reason)
        assertEquals(listOf(0, 1), written)  // chunk 2 bailed before write
    }

    @Test fun `410 with null reason is surfaced as null`() = runTest {
        val outcome = downloadStreamLoop(
            chunkCount = 3,
            downloadChunk = { ApiClient.ChunkDownloadResult.Aborted(null) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamReceiveOutcome.AbortedByUpstream)
        assertNull((outcome as StreamReceiveOutcome.AbortedByUpstream).reason)
    }

    // -----------------------------------------------------------------
    // Network error budget
    // -----------------------------------------------------------------

    @Test fun `3 consecutive NetworkErrors returns RecipientAborted network`() = runTest {
        val clock = VirtualClock()
        val outcome = downloadStreamLoop(
            chunkCount = 1,
            downloadChunk = { ApiClient.ChunkDownloadResult.NetworkError },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = clock.clock,
            sleep = clock.sleep,
        )
        assertTrue(outcome is StreamReceiveOutcome.RecipientAborted)
        assertEquals("network", (outcome as StreamReceiveOutcome.RecipientAborted).reason)
    }

    @Test fun `NetworkError x2 then Ok resets counter`() = runTest {
        val script = Script(
            ApiClient.ChunkDownloadResult.NetworkError,
            ApiClient.ChunkDownloadResult.NetworkError,
            okChunk(0),
            ApiClient.ChunkDownloadResult.NetworkError,
            ApiClient.ChunkDownloadResult.NetworkError,
            okChunk(1),
        )
        val clock = VirtualClock()
        val outcome = downloadStreamLoop(
            chunkCount = 2,
            downloadChunk = { script.next(it) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = clock.clock,
            sleep = clock.sleep,
        )
        assertTrue("chunk 0's 2 net errors + chunk 1's 2 net errors should both recover before 3",
            outcome is StreamReceiveOutcome.Complete)
    }

    @Test fun `ServerError is treated same as NetworkError for budget`() = runTest {
        val outcome = downloadStreamLoop(
            chunkCount = 1,
            downloadChunk = { ApiClient.ChunkDownloadResult.ServerError(502) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamReceiveOutcome.RecipientAborted)
        assertEquals("network", (outcome as StreamReceiveOutcome.RecipientAborted).reason)
    }

    // -----------------------------------------------------------------
    // Auth lost
    // -----------------------------------------------------------------

    @Test fun `AuthError returns AuthLost immediately without sleep`() = runTest {
        val sleeps = mutableListOf<Long>()
        val outcome = downloadStreamLoop(
            chunkCount = 5,
            downloadChunk = { ApiClient.ChunkDownloadResult.AuthError },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = { 0L },
            sleep = { sleeps.add(it) },
        )
        assertTrue(outcome is StreamReceiveOutcome.AuthLost)
        assertTrue("no sleeps on auth bail", sleeps.isEmpty())
    }

    // -----------------------------------------------------------------
    // Decrypt retry
    // -----------------------------------------------------------------

    @Test fun `decrypt failure re-fetches same chunk`() = runTest {
        var decryptCalls = 0
        val script = Script(okChunk(0), okChunk(0), okChunk(1))
        val outcome = downloadStreamLoop(
            chunkCount = 2,
            downloadChunk = { script.next(it) },
            decrypt = {
                decryptCalls++
                if (decryptCalls == 1) null else strippingDecrypt(it)
            },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamReceiveOutcome.Complete)
        // First decrypt returned null → sleep + continue → re-fetched chunk 0.
        assertEquals(2, script.callsFor[0])
        assertEquals(1, script.callsFor[1])
    }

    // -----------------------------------------------------------------
    // Cancellation (row deleted mid-loop)
    // -----------------------------------------------------------------

    @Test fun `isCancelled true bails with recipient_abort`() = runTest {
        var ticks = 0
        val outcome = downloadStreamLoop(
            chunkCount = 5,
            downloadChunk = { okChunk(it) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { _, _ -> },
            ackChunk = { true },
            onProgress = { },
            isCancelled = { ticks++ >= 2 },  // true after 2 checks (chunks 0 and 1 go through)
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamReceiveOutcome.RecipientAborted)
        assertEquals("recipient_abort", (outcome as StreamReceiveOutcome.RecipientAborted).reason)
    }

    // -----------------------------------------------------------------
    // ackChunk failure is non-fatal
    // -----------------------------------------------------------------

    @Test fun `ackChunk returning false does not fail the loop`() = runTest {
        val written = mutableListOf<Int>()
        val outcome = downloadStreamLoop(
            chunkCount = 3,
            downloadChunk = { okChunk(it) },
            decrypt = { strippingDecrypt(it) },
            writeChunk = { i, _ -> written.add(i) },
            ackChunk = { false },  // always fails
            onProgress = { },
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamReceiveOutcome.Complete)
        assertEquals(listOf(0, 1, 2), written)
    }
}
