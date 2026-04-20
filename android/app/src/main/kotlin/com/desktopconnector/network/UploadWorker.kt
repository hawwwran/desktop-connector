package com.desktopconnector.network

import android.content.Context
import android.net.Uri
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

        val initOutcome = api.initTransfer(transferId, transfer.recipientDeviceId, encryptedMeta, chunkCount)
        when (initOutcome) {
            ApiClient.InitOutcome.OK -> { /* proceed below */ }
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
        AppLog.log("Upload", "transfer.init.accepted transfer_id=${transferId.take(12)} recipient=${transfer.recipientDeviceId.take(12)} chunks=$chunkCount")

        try {
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
            AppLog.log("Upload", "transfer.upload.completed transfer_id=${transferId.take(12)} name=${transfer.displayName}")
            return Result.success()
        } catch (e: Exception) {
            Log.e(TAG, "Upload failed: ${e.message}", e)
            AppLog.log("Upload", "transfer.upload.failed transfer_id=${transferId.take(12)} error_kind=${e.javaClass.simpleName}")
            db.transferDao().updateStatus(transferDbId, TransferStatus.FAILED, e.message ?: "Upload error")
            return Result.failure()
        } finally {
            spoolFile?.delete()
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
