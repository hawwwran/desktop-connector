package com.desktopconnector.network

import android.content.Context
import android.net.Uri
import android.os.PowerManager
import android.util.Log
import androidx.work.*
import com.desktopconnector.crypto.CryptoUtils
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.data.AppDatabase
import com.desktopconnector.data.TransferStatus
import kotlinx.coroutines.delay
import java.io.File
import java.io.InputStream
import java.util.UUID
import kotlin.math.max

/**
 * WorkManager worker for background file uploads.
 * Streams each file chunk-by-chunk: read → encrypt → upload. Never holds the
 * whole file in memory.
 */
class UploadWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    companion object {
        private const val TAG = "UploadWorker"
        const val KEY_TRANSFER_DB_ID = "transfer_db_id"
        private const val CHUNK_RETRY_DELAY_MS = 5_000L
        private const val CHUNK_MAX_FAILURE_WINDOW_MS = 120_000L
        private const val INIT_MAX_ATTEMPTS = 3
        // Upper bound on how long a transfer can sit in WAITING state
        // before we give up and mark it FAILED. Mirrors the desktop's
        // STORAGE_FULL_MAX_WINDOW_S cap — prevents a row from
        // zombie-ing if WorkManager keeps rescheduling while the
        // recipient quota never drains.
        private const val STORAGE_FULL_MAX_WINDOW_MS = 30L * 60 * 1000

        fun enqueue(context: Context, transferDbId: Long) {
            val request = OneTimeWorkRequestBuilder<UploadWorker>()
                .setInputData(workDataOf(KEY_TRANSFER_DB_ID to transferDbId))
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                // Tag so TransferViewModel.cancelAndDelete can cancel
                // just this transfer's work chain (prevents a WAITING
                // row we just cancelled from being recreated on
                // WorkManager retry).
                .addTag(tagForTransfer(transferDbId))
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }

        fun tagForTransfer(transferDbId: Long): String = "upload-$transferDbId"
    }

    override suspend fun doWork(): Result {
        val transferDbId = inputData.getLong(KEY_TRANSFER_DB_ID, -1)
        if (transferDbId == -1L) return Result.failure()

        val db = AppDatabase.getInstance(applicationContext)
        val transfer = db.transferDao().getById(transferDbId) ?: return Result.failure()

        val prefs = AppPreferences(applicationContext)
        val serverUrl = prefs.serverUrl ?: return Result.failure()
        val deviceId = prefs.deviceId ?: return Result.failure()
        val authToken = prefs.authToken ?: return Result.failure()

        val keyManager = KeyManager(applicationContext)
        val paired = keyManager.getPairedDevice(transfer.recipientDeviceId) ?: run {
            Log.e(TAG, "No paired device found: ${transfer.recipientDeviceId}")
            db.transferDao().updateStatus(transferDbId, TransferStatus.FAILED, "Paired device not found")
            return Result.failure()
        }

        val api = ApiClient(serverUrl, deviceId, authToken)
        val symmetricKey = android.util.Base64.decode(paired.symmetricKeyB64, android.util.Base64.NO_WRAP)
        val uri = Uri.parse(transfer.contentUri)

        db.transferDao().updateStatus(transferDbId, TransferStatus.PREPARING)

        // Resolve a real file size; spool to cache if URI doesn't expose one.
        var spoolFile: File? = null
        val sourceSize: Long
        val sourceOpener: () -> InputStream
        try {
            if (transfer.sizeBytes > 0L) {
                sourceSize = transfer.sizeBytes
                sourceOpener = {
                    applicationContext.contentResolver.openInputStream(uri)
                        ?: throw java.io.IOException("Cannot open source URI")
                }
            } else {
                val spool = spoolUriToCache(uri, transferDbId)
                spoolFile = spool
                sourceSize = spool.length()
                sourceOpener = { spool.inputStream() }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Prepare failed: ${e.message}", e)
            AppLog.log("Upload", "transfer.init.failed name=${transfer.displayName} error_kind=${e.javaClass.simpleName}")
            db.transferDao().updateStatus(transferDbId, TransferStatus.FAILED, "Cannot read source: ${e.message}")
            return Result.failure()
        }

        val chunkCount = max(1, ((sourceSize + CryptoUtils.CHUNK_SIZE - 1) / CryptoUtils.CHUNK_SIZE).toInt())
        val baseNonce = keyManager.generateBaseNonce()
        val encryptedMeta = keyManager.buildEncryptedMetadata(
            fileName = transfer.displayName,
            mimeType = transfer.mimeType,
            fileSize = sourceSize,
            chunkCount = chunkCount,
            baseNonce = baseNonce,
            symmetricKey = symmetricKey,
        )
        val transferId = UUID.randomUUID().toString()

        // Capability probe. Request streaming only when the server
        // advertises `stream_v1` AND the file isn't a `.fn.*` command
        // (those are force-classic per plan §9 non-goal — too small to
        // benefit, extra round-trips hurt). Server may still downgrade
        // streaming → classic in its response; we honour what it
        // negotiated via `initTransferTyped`'s return.
        val capabilities = try { api.getCapabilities() } catch (_: Exception) { emptySet() }
        val requestedMode = if ("stream_v1" in capabilities && !transfer.displayName.startsWith(".fn.")) {
            "streaming"
        } else {
            "classic"
        }

        val initResult = api.initTransferTyped(
            transferId, transfer.recipientDeviceId, encryptedMeta, chunkCount, requestedMode,
        )
        val initOutcome = initResult.outcome
        val negotiatedMode = initResult.negotiatedMode
        when (initOutcome) {
            ApiClient.InitOutcome.OK -> {
                // Stamp what the server accepted so the history row,
                // D.4b's tracker extension, and D.5's UI all know which
                // branch was taken. Safe to call with negotiatedMode
                // even for classic (it just writes mode=classic).
                db.transferDao().setNegotiatedMode(transferDbId, requestedMode, negotiatedMode)
            }
            ApiClient.InitOutcome.TOO_LARGE -> {
                // 413 — terminal. The transfer is bigger than the
                // server's configured quota; no amount of waiting makes
                // it fit. Mark FAILED immediately with a clear tag.
                AppLog.log("Upload",
                    "transfer.init.too_large transfer_id=${transferId.take(12)}",
                    "error")
                db.transferDao().updateStatus(
                    transferDbId, TransferStatus.FAILED, "exceeds server quota",
                )
                spoolFile?.delete()
                return Result.failure()
            }
            ApiClient.InitOutcome.STORAGE_FULL -> {
                // Recipient's pending-bytes quota is full (earlier
                // transfer still draining). Flip to WAITING so the UI
                // shows a yellow chip + banner instead of the FAILED
                // red it would otherwise get, and let WorkManager
                // reschedule us — up to STORAGE_FULL_MAX_WINDOW_MS,
                // after which we give up and mark FAILED so the row
                // can't zombie forever.
                val rowAgeMs = (System.currentTimeMillis() / 1000 - transfer.createdAt) * 1000
                if (rowAgeMs >= STORAGE_FULL_MAX_WINDOW_MS) {
                    AppLog.log("Upload",
                        "transfer.init.waiting.timed_out transfer_id=${transferId.take(12)} elapsed_ms=$rowAgeMs",
                        "warning")
                    db.transferDao().updateStatus(
                        transferDbId, TransferStatus.FAILED,
                        "quota exceeded",
                    )
                    spoolFile?.delete()
                    return Result.failure()
                }
                AppLog.log("Upload", "transfer.init.waiting transfer_id=${transferId.take(12)} reason=storage_full",
                    "warning")
                db.transferDao().updateStatus(transferDbId, TransferStatus.WAITING)
                StoragePressure.mark()
                spoolFile?.delete()
                return Result.retry()
            }
            ApiClient.InitOutcome.FAILED -> {
                AppLog.log("Upload", "transfer.init.failed transfer_id=${transferId.take(12)} attempt=${runAttemptCount + 1}/$INIT_MAX_ATTEMPTS")
                spoolFile?.delete()
                return if (runAttemptCount + 1 >= INIT_MAX_ATTEMPTS) {
                    db.transferDao().updateStatus(transferDbId, TransferStatus.FAILED, "Failed to initialize transfer on server")
                    Result.failure()
                } else {
                    Result.retry()
                }
            }
        }
        StoragePressure.clear()

        db.transferDao().updateStatus(transferDbId, TransferStatus.UPLOADING)
        db.transferDao().updateProgress(transferDbId, 0, chunkCount)
        AppLog.log("Upload", "transfer.init.accepted transfer_id=${transferId.take(12)} recipient=${transfer.recipientDeviceId.take(12)} chunks=$chunkCount requested_mode=$requestedMode negotiated_mode=$negotiatedMode")

        return try {
            if (negotiatedMode == "streaming") {
                runStreamingUpload(
                    api, transferId, transferDbId, chunkCount, sourceOpener, sourceSize,
                    baseNonce, symmetricKey, keyManager, transfer.displayName, db,
                )
            } else {
                runClassicUpload(
                    api, transferId, transferDbId, chunkCount, sourceOpener, sourceSize,
                    baseNonce, symmetricKey, keyManager, transfer.displayName, db,
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "Upload failed: ${e.message}", e)
            AppLog.log("Upload", "transfer.upload.failed transfer_id=${transferId.take(12)} error_kind=${e.javaClass.simpleName}")
            db.transferDao().updateStatus(transferDbId, TransferStatus.FAILED, e.message ?: "Upload error")
            Result.failure()
        } finally {
            spoolFile?.delete()
        }
    }

    /**
     * Classic upload loop — byte-for-byte the pre-streaming behaviour.
     * Kept separate from the streaming path so D.4a's diff is reviewable
     * without a through-the-middle merge: nothing in this function changes.
     */
    private suspend fun runClassicUpload(
        api: ApiClient,
        transferId: String,
        transferDbId: Long,
        chunkCount: Int,
        sourceOpener: () -> InputStream,
        sourceSize: Long,
        baseNonce: ByteArray,
        symmetricKey: ByteArray,
        keyManager: KeyManager,
        displayName: String,
        db: AppDatabase,
    ): Result {
        sourceOpener().use { input ->
            val buf = ByteArray(CryptoUtils.CHUNK_SIZE)
            for (index in 0 until chunkCount) {
                val plaintext = readFullChunk(input, buf, sourceSize, index, chunkCount)
                val encrypted = keyManager.encryptChunk(plaintext, baseNonce, index, symmetricKey)
                val terminal = uploadChunkWithRetry(api, transferId, index, chunkCount, encrypted)
                if (terminal != null) {
                    AppLog.log("Upload", "transfer.upload.failed transfer_id=${transferId.take(12)} reason=$terminal")
                    db.transferDao().updateStatus(transferDbId, TransferStatus.FAILED, terminal)
                    return Result.failure()
                }
                db.transferDao().updateProgress(transferDbId, index + 1, chunkCount)
            }
        }

        db.transferDao().setTransferId(transferDbId, transferId)
        // Upload logic cleans up its own progress fields; DeliveryTracker owns deliveryChunks/deliveryTotal from here.
        db.transferDao().updateProgress(transferDbId, 0, 0)
        db.transferDao().updateStatus(transferDbId, TransferStatus.COMPLETE)
        AppLog.log("Upload", "transfer.upload.completed transfer_id=${transferId.take(12)} name=$displayName mode=classic")
        return Result.success()
    }

    /**
     * Streaming upload loop (D.4a).
     *
     * Drives chunks through the server via the pure-function state
     * machine [uploadStreamLoop], wrapping it with:
     *   - Wake + WiFi locks for the duration of the loop (mirrors the
     *     receiver's existing download policy). 30 min WAITING_STREAM
     *     windows span well past Android's idle thresholds otherwise.
     *   - Sequential stream reader + encryption per chunk.
     *   - Progress writes to Room every chunk.
     *   - Mapping [StreamOutcome] onto Room status + Worker [Result].
     *
     * Row status transitions in D.4a:
     *   UPLOADING (+ WAITING_STREAM ↔ UPLOADING during 507 episodes)
     *     → COMPLETE on the final chunk's Ok
     *     → ABORTED on recipient DELETE
     *     → FAILED(failureReason=…) on sender give-up.
     *
     * SENDING / delivery-tracker integration is D.4b — the classic
     * delivery tracker picks up COMPLETE+undelivered rows and paints
     * `deliveryChunks` the same way it does for classic transfers.
     */
    private suspend fun runStreamingUpload(
        api: ApiClient,
        transferId: String,
        transferDbId: Long,
        chunkCount: Int,
        sourceOpener: () -> InputStream,
        sourceSize: Long,
        baseNonce: ByteArray,
        symmetricKey: ByteArray,
        keyManager: KeyManager,
        displayName: String,
        db: AppDatabase,
    ): Result {
        // Wake + WiFi locks. Released deterministically in `finally`.
        val pm = applicationContext.getSystemService(Context.POWER_SERVICE) as PowerManager
        val wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DesktopConnector:streaming-upload")
        val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as android.net.wifi.WifiManager
        @Suppress("DEPRECATION")
        val wifiMode = if (android.os.Build.VERSION.SDK_INT >= 29)
            android.net.wifi.WifiManager.WIFI_MODE_FULL_LOW_LATENCY
        else
            android.net.wifi.WifiManager.WIFI_MODE_FULL_HIGH_PERF
        val wifiLock = wifiManager.createWifiLock(wifiMode, "DesktopConnector:streaming-upload")
        wakeLock.acquire(2 * 60 * 1000L)  // 2 min refreshed per chunk
        wifiLock.acquire()

        // Stamp transferId upfront so the delivery tracker (D.4b) can
        // immediately pick this row up via getActiveDeliveryIds once the
        // recipient starts acking chunks. Classic path sets transferId
        // only after the final chunk; streaming needs it available while
        // the upload is still in flight.
        db.transferDao().setTransferId(transferDbId, transferId)

        return try {
            sourceOpener().use { input ->
                val buf = ByteArray(CryptoUtils.CHUNK_SIZE)
                // Pre-encrypt-and-upload per chunk via a closure that the
                // state machine invokes. Keeping it inside sourceOpener's
                // `use {}` means the stream is closed on exit regardless
                // of outcome.
                val outcome = uploadStreamLoop(
                    chunkCount = chunkCount,
                    uploadChunk = { index ->
                        val plaintext = readFullChunk(input, buf, sourceSize, index, chunkCount)
                        val encrypted = keyManager.encryptChunk(plaintext, baseNonce, index, symmetricKey)
                        // Per-chunk wake refresh keeps us alive across
                        // long WAITING_STREAM episodes.
                        wakeLock.acquire(2 * 60 * 1000L)
                        api.uploadChunkTyped(transferId, index, encrypted)
                    },
                    onChunkOk = { index ->
                        db.transferDao().updateProgress(transferDbId, index + 1, chunkCount)
                        // D.4b: flip UPLOADING → SENDING once the tracker
                        // (running on its own thread) has observed any
                        // recipient-side download progress. Idempotent.
                        // Field ownership: status is owned by the upload
                        // loop; we only READ tracker-owned deliveryChunks.
                        maybeFlipToSending(db, transferDbId, isExitingWaitingStream = false)
                    },
                    onEnterWaitingStream = { startedAtMs ->
                        AppLog.log("Upload",
                            "transfer.stream.waiting_quota transfer_id=${transferId.take(12)}",
                            "warning")
                        db.transferDao().markWaitingStream(transferDbId, startedAtMs)
                    },
                    onExitWaitingStream = {
                        // D.4b: decide UPLOADING or SENDING based on whether
                        // the recipient drained anything while we were
                        // blocked on 507. clearWaitingStream() is a simple
                        // status update; the resolved target comes from the
                        // helper so tests can pin both paths.
                        val target = streamingSenderStatusTarget(
                            currentStatus = TransferStatus.WAITING_STREAM,
                            deliveryChunks = db.transferDao().getDeliveryChunks(transferDbId) ?: 0,
                            isExitingWaitingStream = true,
                        ) ?: TransferStatus.UPLOADING
                        db.transferDao().clearWaitingStream(transferDbId, target)
                    },
                )

                when (outcome) {
                    is StreamOutcome.Delivered -> {
                        // D.4b: the sender's last chunk Ok'd. We do NOT flip
                        // to COMPLETE — streaming rows stay SENDING until
                        // the delivery tracker observes downloaded=1 and
                        // calls markDelivered (which only flips the
                        // `delivered` flag, status stays SENDING). The UI
                        // reads `delivered` in addition to `status` to show
                        // "Delivered" post-handoff. Keep `chunksUploaded`/
                        // `totalChunks` intact so the "Sending N→Y" label
                        // renders correctly during the final-ACK window.
                        //
                        // Ensure status is SENDING even in the fast path
                        // where the tracker never saw progress (e.g. very
                        // small transfer, recipient much slower than the
                        // sender's final-chunk POST). Idempotent.
                        maybeFlipToSending(db, transferDbId, isExitingWaitingStream = false,
                                           forceFlip = true)
                        AppLog.log("Upload",
                            "transfer.upload.completed transfer_id=${transferId.take(12)} name=$displayName mode=streaming awaiting_delivery_ack=true")
                        Result.success()
                    }
                    is StreamOutcome.AbortedByRecipient -> {
                        val reason = outcome.reason ?: "recipient_abort"
                        AppLog.log("Upload",
                            "transfer.abort.recipient transfer_id=${transferId.take(12)} reason=$reason")
                        db.transferDao().markAborted(transferDbId, reason)
                        Result.failure()
                    }
                    is StreamOutcome.SenderFailed -> {
                        AppLog.log("Upload",
                            "transfer.upload.failed transfer_id=${transferId.take(12)} reason=${outcome.reason} mode=streaming")
                        // Best-effort tell server so recipient gets 410 on
                        // its next chunk GET instead of 425'ing forever.
                        try { api.abortTransfer(transferId, "sender_failed") } catch (_: Exception) {}
                        db.transferDao().markFailedWithReason(transferDbId, outcome.reason)
                        Result.failure()
                    }
                    is StreamOutcome.AuthLost -> {
                        // Banner latch is already firing via authObservations.
                        // Don't flip the row terminal — let WorkManager retry
                        // once auth recovers (or never, if user re-pairs and
                        // a new transferId is used).
                        AppLog.log("Upload",
                            "transfer.upload.auth_lost transfer_id=${transferId.take(12)} mode=streaming",
                            "warning")
                        Result.retry()
                    }
                }
            }
        } finally {
            if (wakeLock.isHeld) wakeLock.release()
            if (wifiLock.isHeld) wifiLock.release()
        }
    }

    /**
     * Upload one chunk with 5s retry cadence. Returns null on success, or an
     * error message string when the same chunk has been failing continuously
     * for longer than [CHUNK_MAX_FAILURE_WINDOW_MS].
     */
    private suspend fun uploadChunkWithRetry(
        api: ApiClient,
        transferId: String,
        index: Int,
        chunkCount: Int,
        encrypted: ByteArray,
    ): String? {
        var firstFailureAt: Long? = null
        while (true) {
            try {
                val ok = api.uploadChunk(transferId, index, encrypted) != null
                if (ok) {
                    Log.d(TAG, "Uploaded chunk ${index + 1}/$chunkCount")
                    return null
                }
            } catch (e: Exception) {
                Log.w(TAG, "uploadChunk $index threw: ${e.message}")
            }
            val now = System.currentTimeMillis()
            if (firstFailureAt == null) {
                firstFailureAt = now
                Log.w(TAG, "Chunk ${index + 1}/$chunkCount failed, retrying in ${CHUNK_RETRY_DELAY_MS / 1000}s")
            } else if (now - firstFailureAt >= CHUNK_MAX_FAILURE_WINDOW_MS) {
                val seconds = CHUNK_MAX_FAILURE_WINDOW_MS / 1000
                return "Chunk ${index + 1}/$chunkCount failed continuously for ${seconds}s"
            }
            delay(CHUNK_RETRY_DELAY_MS)
        }
    }

    /**
     * D.4b: streaming-sender status-flip helper. Reads the row's current
     * status + tracker-written `deliveryChunks`, computes whether a flip
     * to SENDING is warranted via `streamingSenderStatusTarget`, and
     * writes the flip if so.
     *
     * Called from:
     *   - `onChunkOk`                   (isExitingWaitingStream=false) —
     *     after every successful chunk upload, to catch the first moment
     *     the recipient starts draining.
     *   - `onExitWaitingStream`'s target resolution — not through this
     *     function directly; the callback inlines `streamingSenderStatusTarget`
     *     because the transition there is guaranteed (waiting_stream →
     *     uploading OR sending) and the status write happens via
     *     `clearWaitingStream` which also nulls `waitingStartedAt`.
     *   - `StreamOutcome.Delivered`     (forceFlip=true) — the final
     *     chunk Ok'd. If the tracker never saw progress we still flip
     *     to SENDING so the row enters the "awaiting delivery ack" phase
     *     consistently. `forceFlip` bypasses the deliveryChunks>0 check
     *     so fast-path transfers (upload completes before tracker ticks)
     *     end up in SENDING rather than stuck in UPLOADING.
     *
     * Idempotent. Reads the row + writes status at most once per call.
     */
    private suspend fun maybeFlipToSending(
        db: AppDatabase,
        transferDbId: Long,
        isExitingWaitingStream: Boolean,
        forceFlip: Boolean = false,
    ) {
        val row = db.transferDao().getById(transferDbId) ?: return
        if (row.status == TransferStatus.SENDING) return
        val target = if (forceFlip) {
            TransferStatus.SENDING
        } else {
            streamingSenderStatusTarget(
                currentStatus = row.status,
                deliveryChunks = row.deliveryChunks,
                isExitingWaitingStream = isExitingWaitingStream,
            ) ?: return
        }
        db.transferDao().updateStatus(transferDbId, target, null)
    }

    /** Read exactly one chunk worth of bytes (last chunk may be shorter). */
    private fun readFullChunk(
        input: InputStream,
        buf: ByteArray,
        totalSize: Long,
        index: Int,
        chunkCount: Int,
    ): ByteArray {
        val remaining = totalSize - index.toLong() * CryptoUtils.CHUNK_SIZE
        val target = if (index == chunkCount - 1) {
            // Last chunk: whatever remains, clamped to CHUNK_SIZE. Empty file → one empty chunk.
            max(0L, remaining).coerceAtMost(CryptoUtils.CHUNK_SIZE.toLong()).toInt()
        } else {
            CryptoUtils.CHUNK_SIZE
        }
        if (target == 0) return ByteArray(0)
        var read = 0
        while (read < target) {
            val n = input.read(buf, read, target - read)
            if (n < 0) break
            read += n
        }
        return if (read == buf.size) buf.copyOf() else buf.copyOf(read)
    }

    /** Copy a URI stream to a cache file so we can determine its size and re-read deterministically. */
    private fun spoolUriToCache(uri: Uri, transferDbId: Long): File {
        val spool = File(applicationContext.cacheDir, "upload_spool_$transferDbId.tmp")
        spool.delete()
        val input = applicationContext.contentResolver.openInputStream(uri)
            ?: throw java.io.IOException("Cannot open source URI for spooling")
        input.use { src ->
            spool.outputStream().use { dst -> src.copyTo(dst) }
        }
        return spool
    }
}
