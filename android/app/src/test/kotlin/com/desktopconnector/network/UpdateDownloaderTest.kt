package com.desktopconnector.network

import com.desktopconnector.network.UpdateDownloader.DownloadProgress
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class UpdateDownloaderTest {

    private lateinit var server: MockWebServer
    private lateinit var cacheDir: File
    private val acquireCount = AtomicInteger(0)
    private val releaseCount = AtomicInteger(0)

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        cacheDir = Files.createTempDirectory("update-dl-test").toFile()
        acquireCount.set(0)
        releaseCount.set(0)
    }

    @After fun tearDown() {
        server.shutdown()
        cacheDir.deleteRecursively()
    }

    private fun downloader(): UpdateDownloader = UpdateDownloader(
        cacheDir = cacheDir,
        wakeLockFactory = {
            acquireCount.incrementAndGet()
            UpdateDownloader.WakeLockHolder { releaseCount.incrementAndGet() }
        },
    )

    private fun apkBytes(size: Int): ByteArray = ByteArray(size) { (it % 251).toByte() }

    private fun bodyOf(payload: ByteArray): MockResponse =
        MockResponse()
            .setBody(Buffer().write(payload))
            .setHeader("Content-Length", payload.size.toString())

    // -----------------------------------------------------------------
    // Happy path
    // -----------------------------------------------------------------

    @Test fun `download writes file and emits Done`() = runBlocking {
        val payload = apkBytes(64 * 1024)
        server.enqueue(bodyOf(payload))
        val url = server.url("/Desktop-Connector-0.3.1.apk").toString()

        val events = downloader().download(url, "0.3.1").toList()

        assertTrue("First event should be Started", events.first() is DownloadProgress.Started)
        val done = events.last()
        assertTrue("Last event should be Done, was $done", done is DownloadProgress.Done)
        val target = (done as DownloadProgress.Done).file
        assertEquals("Desktop-Connector-0.3.1.apk", target.name)
        assertEquals(payload.size.toLong(), target.length())
        assertTrue("Bytes should match payload",
            target.readBytes().contentEquals(payload))
        assertEquals(1, acquireCount.get())
        assertEquals(1, releaseCount.get())
    }

    @Test fun `progress events advance bytesRead`() = runBlocking {
        val payload = apkBytes(512 * 1024)  // 512 KB across multiple buffer reads
        server.enqueue(
            bodyOf(payload).throttleBody(64 * 1024L, 50, TimeUnit.MILLISECONDS)
        )
        val url = server.url("/x.apk").toString()

        val events = downloader().download(url, "0.3.1").toList()
        val progresses = events.filterIsInstance<DownloadProgress.Progress>()
        assertTrue("Expected at least one Progress event, got ${progresses.size}",
            progresses.isNotEmpty())
        // Final Progress (the synthetic 100% one emitted just before Done).
        assertEquals(payload.size.toLong(), progresses.last().bytesRead)
        // Bytes-read should never decrease.
        var prev = 0L
        for (p in progresses) {
            assertTrue("bytesRead should be monotonic", p.bytesRead >= prev)
            prev = p.bytesRead
        }
    }

    // -----------------------------------------------------------------
    // Failure paths
    // -----------------------------------------------------------------

    @Test fun `HTTP 404 emits Failed and releases wake lock`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(404))
        val url = server.url("/missing.apk").toString()

        val events = downloader().download(url, "0.3.1").toList()
        val last = events.last()
        assertTrue("Expected Failed, got $last", last is DownloadProgress.Failed)
        assertEquals(1, releaseCount.get())
        val updatesDir = File(cacheDir, "updates")
        assertFalse("No final file",
            File(updatesDir, "Desktop-Connector-0.3.1.apk").exists())
        assertFalse("No partial file",
            File(updatesDir, "Desktop-Connector-0.3.1.apk.partial").exists())
    }

    @Test fun `network failure emits Failed and releases wake lock`() = runBlocking {
        server.shutdown()  // every request fails fast
        val url = server.url("/x.apk").toString()

        val events = downloader().download(url, "0.3.1").toList()
        val last = events.last()
        assertTrue("Expected Failed, got $last", last is DownloadProgress.Failed)
        assertEquals(1, releaseCount.get())
    }

    // -----------------------------------------------------------------
    // Cancellation (acceptance criterion)
    // -----------------------------------------------------------------

    @Test fun `cancellation deletes partial and releases wake lock`() = runBlocking {
        val payload = apkBytes(2 * 1024 * 1024)  // 2 MB
        // Throttle so we have plenty of time to cancel mid-stream.
        server.enqueue(
            bodyOf(payload).throttleBody(8 * 1024L, 50, TimeUnit.MILLISECONDS)
        )
        val url = server.url("/x.apk").toString()

        val collected = mutableListOf<DownloadProgress>()
        val job = launch(Dispatchers.IO) {
            try {
                downloader().download(url, "0.3.1").toList(collected)
            } catch (_: CancellationException) {
                // expected
            }
        }
        // Wait for at least one Progress event before cancelling so we know
        // the body read was definitely in flight.
        withTimeout(5_000) {
            while (collected.none { it is DownloadProgress.Progress }) {
                delay(20)
            }
        }
        job.cancelAndJoin()

        assertEquals("Wake lock released", 1, releaseCount.get())
        val updatesDir = File(cacheDir, "updates")
        val partial = File(updatesDir, "Desktop-Connector-0.3.1.apk.partial")
        val target = File(updatesDir, "Desktop-Connector-0.3.1.apk")
        assertFalse("Partial file should be deleted, exists=${partial.exists()}", partial.exists())
        assertFalse("Target should not be created", target.exists())
    }
}
