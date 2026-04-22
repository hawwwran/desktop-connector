package com.desktopconnector.network

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM unit tests for the D.4a sender streaming state machine.
 *
 * Exercises `uploadStreamLoop` against a fake [ApiClient.ChunkUploadResult]
 * script, a virtual clock, and a sleep shim that advances that clock.
 * No Android runtime, no WorkManager, no network. The goal is to pin
 * every branch the state machine can take — happy path, 507 with
 * recovery, 507 to 30-min timeout, 410 abort, network-error exhaustion,
 * auth lost. If future changes to the loop accidentally break a
 * transition, one of these fails fast.
 */
class UploadStreamLoopTest {

    /** Scripted result provider. Pop the next per-chunk outcome. If
     *  the script is exhausted, the helper re-uses the last entry so
     *  "forever-this-way" scenarios (507-forever, network-forever) can
     *  be written as a single-element script. */
    private class Script(vararg steps: ApiClient.ChunkUploadResult) {
        private val steps = steps.toMutableList()
        var callsFor: MutableMap<Int, Int> = mutableMapOf()
        fun next(index: Int): ApiClient.ChunkUploadResult {
            callsFor[index] = (callsFor[index] ?: 0) + 1
            return if (steps.size > 1) steps.removeAt(0) else steps[0]
        }
    }

    /** Virtual-clock / sleep pair so tests complete in microseconds
     *  even when the state machine would otherwise wait 30 minutes. */
    private class VirtualClock(startMs: Long = 0L) {
        var nowMs: Long = startMs
        val clock: () -> Long = { nowMs }
        val sleep: suspend (Long) -> Unit = { ms -> nowMs += ms }
    }

    // -----------------------------------------------------------------
    // Happy path
    // -----------------------------------------------------------------

    @Test fun `all chunks Ok returns Delivered`() = runTest {
        val script = Script(ApiClient.ChunkUploadResult.Ok)
        val clock = VirtualClock()
        val okIndices = mutableListOf<Int>()
        var enteredWaiting = false
        var exitedWaiting = false

        val outcome = uploadStreamLoop(
            chunkCount = 5,
            uploadChunk = { script.next(it) },
            onChunkOk = { okIndices.add(it) },
            onEnterWaitingStream = { enteredWaiting = true },
            onExitWaitingStream = { exitedWaiting = true },
            clock = clock.clock,
            sleep = clock.sleep,
        )

        assertTrue("expected Delivered, got $outcome", outcome is StreamOutcome.Delivered)
        assertEquals(listOf(0, 1, 2, 3, 4), okIndices)
        assertTrue("should not have entered WAITING_STREAM on happy path", !enteredWaiting)
        assertTrue("should not have exited WAITING_STREAM on happy path", !exitedWaiting)
    }

    @Test fun `empty chunk count short-circuits to Delivered`() = runTest {
        // Defensive: chunkCount 0 should never call the uploader.
        var called = false
        val outcome = uploadStreamLoop(
            chunkCount = 0,
            uploadChunk = { called = true; ApiClient.ChunkUploadResult.Ok },
            onChunkOk = {},
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamOutcome.Delivered)
        assertTrue("uploader must not be called for chunkCount=0", !called)
    }

    // -----------------------------------------------------------------
    // 507 recovery — StorageFull → Ok
    // -----------------------------------------------------------------

    @Test fun `507 twice then Ok recovers and clears waiting state`() = runTest {
        val script = Script(
            ApiClient.ChunkUploadResult.StorageFull,
            ApiClient.ChunkUploadResult.StorageFull,
            ApiClient.ChunkUploadResult.Ok,   // chunk 0 finally succeeds
            ApiClient.ChunkUploadResult.Ok,   // chunk 1
        )
        val clock = VirtualClock()
        var entered = 0
        var exited = 0
        val progress = mutableListOf<Int>()

        val outcome = uploadStreamLoop(
            chunkCount = 2,
            uploadChunk = { script.next(it) },
            onChunkOk = { progress.add(it) },
            onEnterWaitingStream = { entered++ },
            onExitWaitingStream = { exited++ },
            clock = clock.clock,
            sleep = clock.sleep,
        )

        assertTrue("expected Delivered, got $outcome", outcome is StreamOutcome.Delivered)
        assertEquals(listOf(0, 1), progress)
        // Entered waiting exactly once on the first 507, exited exactly
        // once when chunk 0 finally went Ok.
        assertEquals("entered waiting once", 1, entered)
        assertEquals("exited waiting once", 1, exited)
        // Retried chunk 0 three times (2 x StorageFull + 1 x Ok), then
        // chunk 1 once.
        assertEquals(3, script.callsFor[0])
        assertEquals(1, script.callsFor[1])
    }

    @Test fun `507 backoff walks 2-4-8-16-30 cap`() = runTest {
        // Drive purely 507s to verify the backoff ramp. Bail after a
        // few sleeps via a short window so the test terminates.
        val clock = VirtualClock()
        val sleeps = mutableListOf<Long>()
        val captureSleep: suspend (Long) -> Unit = { ms ->
            sleeps.add(ms)
            clock.nowMs += ms
        }

        val outcome = uploadStreamLoop(
            chunkCount = 1,
            uploadChunk = { ApiClient.ChunkUploadResult.StorageFull },
            onChunkOk = {},
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = clock.clock,
            sleep = captureSleep,
            waitingStreamMaxWindowMs = 90_000,  // give up after 90s so we don't loop forever
        )

        assertTrue("eventually gave up", outcome is StreamOutcome.SenderFailed)
        assertEquals("quota_timeout", (outcome as StreamOutcome.SenderFailed).reason)
        // First few sleeps should double until capped at 30_000. We check
        // a prefix of the observed sequence so the test stays robust to
        // the exact number of iterations before the 90s window expires.
        val expectedPrefix = listOf(2_000L, 4_000L, 8_000L, 16_000L, 30_000L)
        assertEquals(expectedPrefix, sleeps.take(5))
        // All subsequent sleeps are capped at 30_000.
        assertTrue(sleeps.drop(5).all { it == 30_000L })
    }

    // -----------------------------------------------------------------
    // 507 to 30-min timeout — the "give up" path
    // -----------------------------------------------------------------

    @Test fun `continuous 507 past 30 min window returns SenderFailed quota_timeout`() = runTest {
        val clock = VirtualClock()

        val outcome = uploadStreamLoop(
            chunkCount = 1,
            uploadChunk = { ApiClient.ChunkUploadResult.StorageFull },
            onChunkOk = {},
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = clock.clock,
            sleep = clock.sleep,
        )

        assertTrue("expected SenderFailed, got $outcome", outcome is StreamOutcome.SenderFailed)
        assertEquals("quota_timeout", (outcome as StreamOutcome.SenderFailed).reason)
        // Virtual clock should now be past 30 min.
        assertTrue("clock advanced past 30 min, got ${clock.nowMs}ms",
            clock.nowMs >= 30L * 60 * 1000)
    }

    // -----------------------------------------------------------------
    // 410 abort — Aborted mid-stream
    // -----------------------------------------------------------------

    @Test fun `410 on chunk 2 returns AbortedByRecipient with reason`() = runTest {
        val script = Script(
            ApiClient.ChunkUploadResult.Ok,
            ApiClient.ChunkUploadResult.Ok,
            ApiClient.ChunkUploadResult.Aborted("recipient_abort"),
        )
        val clock = VirtualClock()
        val progress = mutableListOf<Int>()

        val outcome = uploadStreamLoop(
            chunkCount = 10,
            uploadChunk = { script.next(it) },
            onChunkOk = { progress.add(it) },
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = clock.clock,
            sleep = clock.sleep,
        )

        assertTrue("expected AbortedByRecipient, got $outcome",
            outcome is StreamOutcome.AbortedByRecipient)
        assertEquals("recipient_abort",
            (outcome as StreamOutcome.AbortedByRecipient).reason)
        // Only chunks 0 and 1 should have reported Ok; chunk 2 bailed
        // before progress callback.
        assertEquals(listOf(0, 1), progress)
    }

    @Test fun `410 with null reason is surfaced as null`() = runTest {
        val script = Script(
            ApiClient.ChunkUploadResult.Aborted(null),
        )
        val outcome = uploadStreamLoop(
            chunkCount = 3,
            uploadChunk = { script.next(it) },
            onChunkOk = {},
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = { 0L },
            sleep = {},
        )
        assertTrue(outcome is StreamOutcome.AbortedByRecipient)
        assertNull((outcome as StreamOutcome.AbortedByRecipient).reason)
    }

    // -----------------------------------------------------------------
    // Network exhaustion
    // -----------------------------------------------------------------

    @Test fun `continuous NetworkError past 120s returns SenderFailed network`() = runTest {
        val clock = VirtualClock()

        val outcome = uploadStreamLoop(
            chunkCount = 1,
            uploadChunk = { ApiClient.ChunkUploadResult.NetworkError },
            onChunkOk = {},
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = clock.clock,
            sleep = clock.sleep,
        )

        assertTrue("expected SenderFailed, got $outcome", outcome is StreamOutcome.SenderFailed)
        assertEquals("network", (outcome as StreamOutcome.SenderFailed).reason)
        // 120 s budget with 5 s cadence → ~24 retries.
        assertTrue("clock past 120s", clock.nowMs >= 120_000)
    }

    @Test fun `brief NetworkError then Ok recovers without giving up`() = runTest {
        val script = Script(
            ApiClient.ChunkUploadResult.NetworkError,  // retry #1
            ApiClient.ChunkUploadResult.NetworkError,  // retry #2
            ApiClient.ChunkUploadResult.Ok,            // eventually works
            ApiClient.ChunkUploadResult.Ok,            // chunk 1
        )
        val clock = VirtualClock()
        val progress = mutableListOf<Int>()

        val outcome = uploadStreamLoop(
            chunkCount = 2,
            uploadChunk = { script.next(it) },
            onChunkOk = { progress.add(it) },
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = clock.clock,
            sleep = clock.sleep,
        )

        assertTrue("expected Delivered, got $outcome", outcome is StreamOutcome.Delivered)
        assertEquals(listOf(0, 1), progress)
    }

    @Test fun `ServerError is treated same as NetworkError for budget accounting`() = runTest {
        val clock = VirtualClock()
        val outcome = uploadStreamLoop(
            chunkCount = 1,
            uploadChunk = { ApiClient.ChunkUploadResult.ServerError(502) },
            onChunkOk = {},
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = clock.clock,
            sleep = clock.sleep,
        )
        assertTrue(outcome is StreamOutcome.SenderFailed)
        assertEquals("network", (outcome as StreamOutcome.SenderFailed).reason)
    }

    // -----------------------------------------------------------------
    // Auth lost
    // -----------------------------------------------------------------

    @Test fun `AuthError returns AuthLost immediately without retry`() = runTest {
        val script = Script(
            ApiClient.ChunkUploadResult.AuthError,
        )
        val sleeps = mutableListOf<Long>()

        val outcome = uploadStreamLoop(
            chunkCount = 5,
            uploadChunk = { script.next(it) },
            onChunkOk = {},
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = { 0L },
            sleep = { sleeps.add(it) },
        )

        assertTrue(outcome is StreamOutcome.AuthLost)
        assertTrue("no sleeps on auth bail", sleeps.isEmpty())
        // AuthError must not trigger waiting/backoff — only one call per
        // the retry budget.
        assertEquals(1, script.callsFor[0])
    }

    // -----------------------------------------------------------------
    // Interleaving — recovery cross-checks
    // -----------------------------------------------------------------

    @Test fun `Ok resets network retry budget so later flakiness isn't counted cumulatively`() = runTest {
        // Verify that the network-error budget resets on a successful
        // chunk. Without reset, a 60 s flake early + 70 s flake later
        // would wrongly trip the 120 s budget.
        val script = Script(
            ApiClient.ChunkUploadResult.NetworkError,  // start flake 1
            ApiClient.ChunkUploadResult.Ok,            // recover chunk 0
            ApiClient.ChunkUploadResult.NetworkError,  // start flake 2 for chunk 1
            ApiClient.ChunkUploadResult.NetworkError,
            ApiClient.ChunkUploadResult.Ok,            // recover chunk 1
        )
        val clock = VirtualClock()

        val outcome = uploadStreamLoop(
            chunkCount = 2,
            uploadChunk = { script.next(it) },
            onChunkOk = {},
            onEnterWaitingStream = {},
            onExitWaitingStream = {},
            clock = clock.clock,
            sleep = clock.sleep,
            networkErrorBudgetMs = 30_000,  // tight for the test
        )

        assertTrue("expected Delivered, got $outcome", outcome is StreamOutcome.Delivered)
    }
}
