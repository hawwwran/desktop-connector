package com.desktopconnector.network

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * JVM unit tests for the auto-update version-check engine.
 *
 * Pins the load-bearing decisions: tag-prefix filtering, version
 * compare semantics, cache freshness, If-Modified-Since 304 replay,
 * stale-cache fallback on network failure, fail-fast timeouts, and
 * coroutine-cancellation cooperation.
 */
class UpdateCheckerTest {

    private lateinit var server: MockWebServer
    private lateinit var cacheFile: File

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        cacheFile = File.createTempFile("update-check", ".json").also { it.delete() }
    }

    @After fun tearDown() {
        server.shutdown()
        cacheFile.delete()
    }

    // -----------------------------------------------------------------
    // Version compare (companion-level)
    // -----------------------------------------------------------------

    @Test fun `version compare picks newer minor`() {
        assertTrue(UpdateChecker.isNewerVersion("0.3.1", "0.3.0"))
    }

    @Test fun `version compare picks newer major`() {
        assertTrue(UpdateChecker.isNewerVersion("1.0.0", "0.9.9"))
    }

    @Test fun `version compare returns false for equal`() {
        assertFalse(UpdateChecker.isNewerVersion("0.3.0", "0.3.0"))
    }

    @Test fun `version compare returns false for older`() {
        assertFalse(UpdateChecker.isNewerVersion("0.2.9", "0.3.0"))
    }

    @Test fun `version compare returns false for unparseable`() {
        // pre-release tags like 0.3.0-rc.1 must not crash and must not
        // be treated as newer than the current stable.
        assertFalse(UpdateChecker.isNewerVersion("0.3.0-rc.1", "0.3.0"))
        assertFalse(UpdateChecker.isNewerVersion("0.3.0", "0.3.0-rc.1"))
    }

    @Test fun `version compare handles different lengths`() {
        // 0.3 vs 0.3.0 → equal; 0.3.1 vs 0.3 → newer.
        assertFalse(UpdateChecker.isNewerVersion("0.3", "0.3.0"))
        assertTrue(UpdateChecker.isNewerVersion("0.3.1", "0.3"))
    }

    // -----------------------------------------------------------------
    // Happy path
    // -----------------------------------------------------------------

    @Test fun `newer release returns isNewer true and stale false`() = runTest {
        server.enqueue(MockResponse().setBody(releasesJson(release(tag = "android/v0.3.1"))))
        val info = checker(currentVersion = "0.3.0").check()
        assertNotNull(info)
        assertEquals("0.3.1", info!!.latestVersion)
        assertEquals("0.3.0", info.currentVersion)
        assertTrue(info.isNewer)
        assertFalse(info.stale)
        assertTrue(info.apkUrl.startsWith("https://example.com/"))
    }

    @Test fun `equal version returns isNewer false`() = runTest {
        server.enqueue(MockResponse().setBody(releasesJson(release(tag = "android/v0.3.0"))))
        val info = checker(currentVersion = "0.3.0").check()
        assertNotNull(info)
        assertFalse(info!!.isNewer)
    }

    @Test fun `older version returns isNewer false`() = runTest {
        server.enqueue(MockResponse().setBody(releasesJson(release(tag = "android/v0.2.0"))))
        val info = checker(currentVersion = "0.3.0").check()
        assertNotNull(info)
        assertFalse(info!!.isNewer)
    }

    // -----------------------------------------------------------------
    // Filtering
    // -----------------------------------------------------------------

    @Test fun `draft and prerelease entries are skipped`() = runTest {
        server.enqueue(MockResponse().setBody(releasesJson(
            release(tag = "android/v0.4.0", draft = true),
            release(tag = "android/v0.3.5", prerelease = true),
            release(tag = "android/v0.3.1"),
        )))
        val info = checker(currentVersion = "0.3.0").check()
        assertNotNull(info)
        assertEquals("0.3.1", info!!.latestVersion)
    }

    @Test fun `non-android tags are skipped`() = runTest {
        // The releases page mixes desktop, server, and android tags. The
        // checker must return the first android one regardless of order.
        server.enqueue(MockResponse().setBody(releasesJson(
            release(tag = "desktop/v0.3.5"),
            release(tag = "server/v0.4.0"),
            release(tag = "android/v0.3.1"),
        )))
        val info = checker(currentVersion = "0.3.0").check()
        assertNotNull(info)
        assertEquals("0.3.1", info!!.latestVersion)
    }

    @Test fun `release with no apk asset is skipped`() = runTest {
        server.enqueue(MockResponse().setBody(releasesJson(
            release(tag = "android/v0.4.0", apkUrl = null),
            release(tag = "android/v0.3.1"),
        )))
        val info = checker(currentVersion = "0.3.0").check()
        assertNotNull(info)
        assertEquals("0.3.1", info!!.latestVersion)
    }

    @Test fun `no android releases at all returns null`() = runTest {
        server.enqueue(MockResponse().setBody(releasesJson(
            release(tag = "desktop/v0.3.5"),
            release(tag = "server/v0.4.0"),
        )))
        val info = checker(currentVersion = "0.3.0").check()
        assertNull(info)
    }

    // -----------------------------------------------------------------
    // Caching
    // -----------------------------------------------------------------

    @Test fun `cache hit within 24 hours skips network`() = runTest {
        // First call populates the cache.
        server.enqueue(MockResponse()
            .setBody(releasesJson(release(tag = "android/v0.3.1")))
            .setHeader("Last-Modified", "Sun, 27 Apr 2026 16:53:43 GMT"))
        val first = checker(currentVersion = "0.3.0").check()
        assertNotNull(first)
        assertEquals(1, server.requestCount)

        // Second call within 24 h must NOT hit the network.
        val second = checker(currentVersion = "0.3.0").check()
        assertNotNull(second)
        assertEquals("0.3.1", second!!.latestVersion)
        assertFalse(second.stale)
        assertEquals(1, server.requestCount)
    }

    @Test fun `force=true bypasses cache freshness`() = runTest {
        server.enqueue(MockResponse().setBody(releasesJson(release(tag = "android/v0.3.1"))))
        checker().check()  // populates cache
        assertEquals(1, server.requestCount)

        // Force-check must hit the network even within 24 h.
        server.enqueue(MockResponse().setBody(releasesJson(release(tag = "android/v0.3.2"))))
        val info = checker().check(force = true)
        assertEquals(2, server.requestCount)
        assertEquals("0.3.2", info!!.latestVersion)
    }

    @Test fun `304 response replays cache and stays non-stale`() = runTest {
        var fakeTime = 1_000L
        val timeProvider = { fakeTime }
        // First populate the cache.
        server.enqueue(MockResponse()
            .setBody(releasesJson(release(tag = "android/v0.3.1")))
            .setHeader("Last-Modified", "Sun, 27 Apr 2026 16:53:43 GMT"))
        val first = checker(currentVersion = "0.3.0", now = timeProvider).check()
        assertNotNull(first)

        // Advance past the 24 h window so the next call hits the network.
        fakeTime += 25L * 60 * 60 * 1000

        // Server replies 304 — the checker must replay the cache.
        server.enqueue(MockResponse().setResponseCode(304))
        val second = checker(currentVersion = "0.3.0", now = timeProvider).check()
        assertNotNull(second)
        assertEquals("0.3.1", second!!.latestVersion)
        assertFalse(second.stale)
        assertEquals(2, server.requestCount)

        // The second request must have carried If-Modified-Since.
        val firstReq = server.takeRequest()
        val secondReq = server.takeRequest()
        assertNull(firstReq.getHeader("If-Modified-Since"))
        assertEquals("Sun, 27 Apr 2026 16:53:43 GMT", secondReq.getHeader("If-Modified-Since"))
    }

    // -----------------------------------------------------------------
    // Network failure paths
    // -----------------------------------------------------------------

    @Test fun `network failure without cache returns null`() = runTest {
        server.shutdown()  // every request fails fast
        val info = checker().check()
        assertNull(info)
    }

    @Test fun `network failure with stale cache returns stale info`() = runTest {
        var fakeTime = 1_000L
        val timeProvider = { fakeTime }
        // Populate the cache.
        server.enqueue(MockResponse().setBody(releasesJson(release(tag = "android/v0.3.1"))))
        checker(currentVersion = "0.3.0", now = timeProvider).check()

        // Advance past 24 h so the next call goes to the network.
        fakeTime += 25L * 60 * 60 * 1000

        // Server is down for the second call.
        server.shutdown()
        val info = checker(currentVersion = "0.3.0", now = timeProvider).check()
        assertNotNull(info)
        assertEquals("0.3.1", info!!.latestVersion)
        assertTrue(info.stale)
    }

    // -----------------------------------------------------------------
    // Fail-fast / cancellation
    // -----------------------------------------------------------------

    @Test fun `slow server returns null within callTimeout budget`() = runBlocking {
        // Use a tighter test client so we don't burn the production 8 s
        // budget on every CI run. The point is: callTimeout is honoured
        // and the suspend doesn't hang for the body delay.
        val tight = OkHttpClient.Builder()
            .connectTimeout(100, TimeUnit.MILLISECONDS)
            .readTimeout(200, TimeUnit.MILLISECONDS)
            .callTimeout(300, TimeUnit.MILLISECONDS)
            .build()
        server.enqueue(MockResponse()
            .setBody(releasesJson(release(tag = "android/v0.3.1")))
            .setBodyDelay(2, TimeUnit.SECONDS))

        val start = System.currentTimeMillis()
        val info = UpdateChecker(
            cacheFile = cacheFile,
            currentVersion = "0.3.0",
            isDebugBuild = false,
            baseUrl = baseUrl(),
            httpClient = tight,
        ).check()
        val elapsed = System.currentTimeMillis() - start

        assertNull(info)
        assertTrue("Expected return within ~300 ms callTimeout, got ${elapsed}ms",
            elapsed < 1500)
    }

    @Test fun `coroutine cancellation aborts in-flight check promptly`() = runBlocking {
        // Slow server response so the call is in flight when we cancel.
        server.enqueue(MockResponse()
            .setBody(releasesJson(release(tag = "android/v0.3.1")))
            .setBodyDelay(5, TimeUnit.SECONDS))

        var caughtCancellation = false
        val start = System.currentTimeMillis()
        val job = launch(Dispatchers.IO) {
            try {
                checker().check()
            } catch (e: CancellationException) {
                caughtCancellation = true
                throw e
            }
        }
        delay(100)  // let the request kick off
        job.cancelAndJoin()
        val elapsed = System.currentTimeMillis() - start

        assertTrue("Cancellation should be prompt (< 1.5 s), took ${elapsed}ms",
            elapsed < 1500)
        assertTrue("CancellationException should propagate", caughtCancellation)
    }

    // -----------------------------------------------------------------
    // Build gate
    // -----------------------------------------------------------------

    @Test fun `debug build short-circuits to null`() = runTest {
        server.enqueue(MockResponse().setBody(releasesJson(release(tag = "android/v0.3.1"))))
        val info = UpdateChecker(
            cacheFile = cacheFile,
            currentVersion = "0.3.0",
            isDebugBuild = true,  // gate ON
            baseUrl = baseUrl(),
        ).check()
        assertNull(info)
        assertEquals("Debug build must not hit the network", 0, server.requestCount)
    }

    // -----------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------

    private fun checker(
        currentVersion: String = "0.3.0",
        now: () -> Long = { System.currentTimeMillis() },
    ) = UpdateChecker(
        cacheFile = cacheFile,
        currentVersion = currentVersion,
        isDebugBuild = false,
        baseUrl = baseUrl(),
        now = now,
    )

    private fun baseUrl(): String = server.url("/").toString().trimEnd('/')

    private fun release(
        tag: String,
        draft: Boolean = false,
        prerelease: Boolean = false,
        apkUrl: String? = "https://example.com/${tag.substringAfterLast('/')}.apk",
        body: String = "## Changes\nFoo bar baz",
    ): JSONObject = JSONObject().apply {
        put("tag_name", tag)
        put("html_url", "https://github.com/hawwwran/desktop-connector/releases/tag/$tag")
        put("draft", draft)
        put("prerelease", prerelease)
        put("body", body)
        val assets = JSONArray()
        if (apkUrl != null) {
            assets.put(JSONObject()
                .put("name", "Desktop-Connector-${tag.substringAfterLast('v')}-release.apk")
                .put("browser_download_url", apkUrl))
        }
        put("assets", assets)
    }

    private fun releasesJson(vararg releases: JSONObject): String {
        val arr = JSONArray()
        releases.forEach { arr.put(it) }
        return arr.toString()
    }
}
