package com.desktopconnector.network

import android.content.Context
import android.os.PowerManager
import com.desktopconnector.data.AppLog
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okio.buffer
import okio.sink
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * Streams an APK from a URL to the app's cache directory, emitting
 * progress events as a cold Flow. Cancel the collecting coroutine to
 * abort — partial file deleted, wake lock released, no leftover state.
 *
 * Holds a `PARTIAL_WAKE_LOCK` for the duration so a screen-off pause
 * doesn't stall the download. The wake-lock acquisition lives behind
 * a factory lambda so JVM tests can supply a fake.
 *
 * Pure of UI / Compose deps.
 */
class UpdateDownloader(
    private val cacheDir: File,
    private val wakeLockFactory: () -> WakeLockHolder,
    httpClient: OkHttpClient? = null,
) {
    fun interface WakeLockHolder {
        fun release()
    }

    sealed class DownloadProgress {
        object Started : DownloadProgress()
        data class Progress(val bytesRead: Long, val total: Long) : DownloadProgress()
        data class Done(val file: File) : DownloadProgress()
        data class Failed(val reason: String) : DownloadProgress()
    }

    private val client: OkHttpClient = httpClient ?: OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        // No callTimeout — large APKs over slow networks can legitimately
        // take minutes; readTimeout protects against a stalled socket.
        .build()

    fun download(url: String, version: String): Flow<DownloadProgress> = flow {
        emit(DownloadProgress.Started)
        AppLog.log("UpdateDownload", "Start url=$url version=$version")

        val targetDir = File(cacheDir, "updates").apply { mkdirs() }
        val target = File(targetDir, "Desktop-Connector-$version.apk")
        val partial = File(targetDir, target.name + ".partial")
        partial.delete()

        val wakeLock = wakeLockFactory()
        var success = false
        try {
            val request = Request.Builder().url(url).build()
            awaitResponse(request).use { resp ->
                if (!resp.isSuccessful) {
                    emit(DownloadProgress.Failed("HTTP ${resp.code}"))
                    return@flow
                }
                val body = resp.body ?: run {
                    emit(DownloadProgress.Failed("Empty response body"))
                    return@flow
                }
                val total = body.contentLength()
                var bytesRead = 0L
                var lastEmitAt = 0L
                val temp = okio.Buffer()
                partial.sink().buffer().use { sink ->
                    val source = body.source()
                    while (true) {
                        currentCoroutineContext().ensureActive()
                        val read = source.read(temp, BUFFER_SIZE)
                        if (read == -1L) break
                        sink.write(temp, read)
                        bytesRead += read
                        val now = System.currentTimeMillis()
                        if (now - lastEmitAt >= PROGRESS_EMIT_INTERVAL_MS) {
                            emit(DownloadProgress.Progress(bytesRead, total))
                            lastEmitAt = now
                        }
                    }
                }
                currentCoroutineContext().ensureActive()
                // Truncation check — protect against connection drops the
                // server reports as a clean EOF. If the response advertised
                // a Content-Length and we got fewer bytes, the file on disk
                // is a partial APK that the system installer will reject as
                // "package invalid". Fail loudly here instead.
                if (total > 0 && bytesRead != total) {
                    AppLog.log(
                        "UpdateDownload",
                        "Truncated: read=$bytesRead expected=$total",
                        "warning",
                    )
                    emit(DownloadProgress.Failed(
                        "Download incomplete (${bytesRead}/${total} bytes)"
                    ))
                    return@flow
                }
                if (!partial.renameTo(target)) {
                    emit(DownloadProgress.Failed("Could not finalize update file"))
                    return@flow
                }
                success = true
                emit(DownloadProgress.Progress(bytesRead, total))
                emit(DownloadProgress.Done(target))
                AppLog.log("UpdateDownload", "Done size=$bytesRead path=${target.name}")
            }
        } catch (e: CancellationException) {
            AppLog.log("UpdateDownload", "Cancelled")
            throw e
        } catch (e: Exception) {
            AppLog.log("UpdateDownload", "Error: ${e.message}")
            emit(DownloadProgress.Failed(e.message ?: e.javaClass.simpleName))
        } finally {
            if (!success) partial.delete()
            wakeLock.release()
        }
    }.flowOn(Dispatchers.IO)

    private suspend fun awaitResponse(request: Request): Response =
        suspendCancellableCoroutine { cont ->
            val call = client.newCall(request)
            cont.invokeOnCancellation { runCatching { call.cancel() } }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (cont.isActive) cont.resumeWithException(e)
                }
                override fun onResponse(call: Call, response: Response) {
                    if (cont.isActive) cont.resume(response)
                    else response.close()
                }
            })
        }

    companion object {
        private const val BUFFER_SIZE = 64L * 1024L
        private const val PROGRESS_EMIT_INTERVAL_MS = 100L
        private const val WAKE_LOCK_TIMEOUT_MS = 30L * 60 * 1000  // safety cap

        /**
         * Production factory: wires in an Android `PARTIAL_WAKE_LOCK` and
         * uses the app's `cacheDir`.
         */
        fun forContext(context: Context, httpClient: OkHttpClient? = null) = UpdateDownloader(
            cacheDir = context.cacheDir,
            wakeLockFactory = {
                val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
                val wl = pm.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK,
                    "DesktopConnector::UpdateDownload"
                ).apply {
                    setReferenceCounted(false)
                    acquire(WAKE_LOCK_TIMEOUT_MS)
                }
                WakeLockHolder { if (wl.isHeld) wl.release() }
            },
            httpClient = httpClient,
        )
    }
}
