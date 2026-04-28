package com.desktopconnector.network

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/**
 * Polls GitHub Releases for `android/v*` tags newer than the running
 * app's versionName. Caches the response on disk for 24 h and uses
 * `If-Modified-Since` so re-checks within that window cost zero
 * network bytes.
 *
 * Aggressive timeouts (3 s connect / 5 s read / 8 s overall) and
 * coroutine-cancellation cooperation guarantee `check()` never
 * blocks longer than ~8 s, and aborts immediately when the activity
 * backgrounds (`MainActivity.onStop` cancels the job).
 *
 * Pure of UI / Compose dependencies so it can be JVM-unit-tested
 * with MockWebServer. The `isDebugBuild` gate short-circuits checks
 * on dev builds — they have a different signature and couldn't apply
 * a release APK anyway.
 */
class UpdateChecker(
    private val cacheFile: File,
    private val currentVersion: String,
    private val isDebugBuild: Boolean,
    private val baseUrl: String = DEFAULT_BASE_URL,
    httpClient: OkHttpClient? = null,
    private val now: () -> Long = { System.currentTimeMillis() },
) {
    private val client: OkHttpClient = httpClient ?: OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(5, TimeUnit.SECONDS)
        .callTimeout(8, TimeUnit.SECONDS)
        .build()

    suspend fun check(force: Boolean = false): UpdateInfo? {
        if (isDebugBuild) return null

        val cached = withContext(Dispatchers.IO) { readCache() }

        if (!force && cached != null && (now() - cached.fetchedAt) in 0 until CACHE_TTL_MS) {
            return buildInfo(cached, stale = false)
        }

        val request = buildRequest(cached?.lastModified)
        return executeAndProcess(request, cached)
    }

    /**
     * Single suspendCancellableCoroutine spans the full lifecycle of
     * the call (request → response → body read → cache write) so
     * `invokeOnCancellation` survives across the body read. Without
     * this, cancellation during the blocking `body.string()` would
     * have to wait for OkHttp's `callTimeout` to fire.
     */
    private suspend fun executeAndProcess(
        request: Request,
        cached: CachedRelease?,
    ): UpdateInfo? = suspendCancellableCoroutine { cont ->
        val call = client.newCall(request)
        cont.invokeOnCancellation { runCatching { call.cancel() } }

        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (!cont.isActive) return
                cont.resume(cached?.let { buildInfo(it, stale = true) })
            }

            override fun onResponse(call: Call, response: Response) {
                if (!cont.isActive) {
                    response.close()
                    return
                }
                val info: UpdateInfo? = try {
                    response.use { resp -> processResponse(resp, cached) }
                } catch (_: IOException) {
                    cached?.let { buildInfo(it, stale = true) }
                } catch (e: CancellationException) {
                    // Surfaces if writeCache somehow throws CE — propagate
                    // to the suspend so the caller's cancellation flag fires.
                    if (cont.isActive) cont.cancel(e)
                    return
                }
                if (cont.isActive) cont.resume(info)
            }
        })
    }

    private fun processResponse(resp: Response, cached: CachedRelease?): UpdateInfo? {
        return when (resp.code) {
            304 -> {
                if (cached != null) {
                    writeCache(cached.copy(fetchedAt = now()))
                    buildInfo(cached, stale = false)
                } else null
            }
            200 -> handleOk(resp, cached)
            else -> cached?.let { buildInfo(it, stale = true) }
        }
    }

    private fun handleOk(resp: Response, cached: CachedRelease?): UpdateInfo? {
        val body = resp.body?.string()
        if (body.isNullOrEmpty()) return cached?.let { buildInfo(it, stale = true) }
        // Don't drop a working cache for a transient "no android release
        // on this page" answer — leave the cache file untouched, return
        // null so the caller doesn't surface an update either way.
        val parsed = parseRelease(body) ?: return null
        val fresh = CachedRelease(
            fetchedAt = now(),
            lastModified = resp.header("Last-Modified") ?: "",
            release = parsed,
        )
        writeCache(fresh)
        return buildInfo(fresh, stale = false)
    }

    private fun buildRequest(lastModified: String?): Request {
        val builder = Request.Builder()
            .url("$baseUrl/repos/$REPO/releases?per_page=$PER_PAGE")
            .header("Accept", "application/vnd.github+json")
            .header("User-Agent", USER_AGENT)
        if (!lastModified.isNullOrEmpty()) {
            builder.header("If-Modified-Since", lastModified)
        }
        return builder.build()
    }

    private fun buildInfo(cached: CachedRelease, stale: Boolean): UpdateInfo? {
        val r = cached.release
        if (!r.tagName.startsWith(TAG_PREFIX)) return null
        val latest = r.tagName.removePrefix(TAG_PREFIX)
        if (r.apkUrl.isEmpty()) return null
        return UpdateInfo(
            currentVersion = currentVersion,
            latestVersion = latest,
            releaseUrl = r.htmlUrl,
            apkUrl = r.apkUrl,
            releaseNotes = r.body,
            isNewer = isNewerVersion(latest, currentVersion),
            stale = stale,
        )
    }

    private fun parseRelease(body: String): ParsedRelease? {
        return try {
            val arr = JSONArray(body)
            for (i in 0 until arr.length()) {
                val item = arr.optJSONObject(i) ?: continue
                if (item.optBoolean("draft", false)) continue
                if (item.optBoolean("prerelease", false)) continue
                val tag = item.optString("tag_name", "")
                if (!tag.startsWith(TAG_PREFIX)) continue
                val apkUrl = extractApkUrl(item.optJSONArray("assets")) ?: continue
                return ParsedRelease(
                    tagName = tag,
                    htmlUrl = item.optString("html_url", ""),
                    apkUrl = apkUrl,
                    body = item.optString("body", ""),
                )
            }
            null
        } catch (_: Exception) {
            null
        }
    }

    private fun extractApkUrl(assets: JSONArray?): String? {
        if (assets == null) return null
        for (i in 0 until assets.length()) {
            val asset = assets.optJSONObject(i) ?: continue
            if (asset.optString("name", "").endsWith(".apk", ignoreCase = true)) {
                val url = asset.optString("browser_download_url", "")
                if (url.isNotEmpty()) return url
            }
        }
        return null
    }

    private fun readCache(): CachedRelease? {
        if (!cacheFile.exists()) return null
        return try {
            val obj = JSONObject(cacheFile.readText())
            val release = obj.optJSONObject("release") ?: return null
            CachedRelease(
                fetchedAt = obj.optLong("fetched_at", 0L),
                lastModified = obj.optString("last_modified", ""),
                release = ParsedRelease(
                    tagName = release.optString("tag_name", ""),
                    htmlUrl = release.optString("html_url", ""),
                    apkUrl = release.optString("apk_url", ""),
                    body = release.optString("body", ""),
                ),
            )
        } catch (_: Exception) {
            null
        }
    }

    private fun writeCache(cached: CachedRelease) {
        try {
            cacheFile.parentFile?.mkdirs()
            val release = JSONObject()
                .put("tag_name", cached.release.tagName)
                .put("html_url", cached.release.htmlUrl)
                .put("apk_url", cached.release.apkUrl)
                .put("body", cached.release.body)
            val payload = JSONObject()
                .put("fetched_at", cached.fetchedAt)
                .put("last_modified", cached.lastModified)
                .put("release", release)
            val tmp = File(cacheFile.absolutePath + ".tmp")
            tmp.writeText(payload.toString())
            tmp.renameTo(cacheFile)
        } catch (_: Exception) {
            // Best-effort: a failed cache write means we'll re-fetch next time.
        }
    }

    private data class CachedRelease(
        val fetchedAt: Long,
        val lastModified: String,
        val release: ParsedRelease,
    )

    private data class ParsedRelease(
        val tagName: String,
        val htmlUrl: String,
        val apkUrl: String,
        val body: String,
    )

    companion object {
        private const val DEFAULT_BASE_URL = "https://api.github.com"
        private const val REPO = "hawwwran/desktop-connector"
        private const val PER_PAGE = 30
        internal const val TAG_PREFIX = "android/v"
        private const val USER_AGENT = "desktop-connector-android-updater"
        private const val CACHE_TTL_MS = 24L * 60L * 60L * 1000L

        /**
         * Naive dotted-int compare. Returns false on any non-int component
         * so an unrecognised tag (`android/v0.3.0-rc.1`) is treated as
         * NOT newer rather than crashing on `toInt()`.
         */
        fun isNewerVersion(latest: String, current: String): Boolean {
            val lp = parseDottedInts(latest) ?: return false
            val cp = parseDottedInts(current) ?: return false
            return compareLists(lp, cp) > 0
        }

        private fun parseDottedInts(s: String): List<Int>? {
            val parts = mutableListOf<Int>()
            for (piece in s.split('.')) {
                parts.add(piece.toIntOrNull() ?: return null)
            }
            return parts
        }

        private fun compareLists(a: List<Int>, b: List<Int>): Int {
            val n = maxOf(a.size, b.size)
            for (i in 0 until n) {
                val ai = a.getOrElse(i) { 0 }
                val bi = b.getOrElse(i) { 0 }
                if (ai != bi) return ai.compareTo(bi)
            }
            return 0
        }
    }
}
