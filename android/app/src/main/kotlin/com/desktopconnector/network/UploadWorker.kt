package com.desktopconnector.network

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Log
import androidx.work.*
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.data.AppDatabase
import com.desktopconnector.data.TransferStatus
import java.util.UUID

/**
 * WorkManager worker for background file uploads.
 * Processes the transfer queue: encrypts, chunks, uploads each file.
 */
class UploadWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    companion object {
        private const val TAG = "UploadWorker"
        const val KEY_TRANSFER_DB_ID = "transfer_db_id"

        fun enqueue(context: Context, transferDbId: Long) {
            val request = OneTimeWorkRequestBuilder<UploadWorker>()
                .setInputData(workDataOf(KEY_TRANSFER_DB_ID to transferDbId))
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }
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

        // Update status + show blue dot
        db.transferDao().updateStatus(transferDbId, TransferStatus.UPLOADING)

        try {
            // Read file
            val uri = Uri.parse(transfer.contentUri)
            val inputStream = applicationContext.contentResolver.openInputStream(uri)
                ?: throw Exception("Cannot open file: ${transfer.contentUri}")

            // Encrypt
            val result = keyManager.encryptFileToChunks(
                inputStream = inputStream,
                fileSize = transfer.sizeBytes,
                fileName = transfer.displayName,
                mimeType = transfer.mimeType,
                symmetricKey = symmetricKey,
            )
            inputStream.close()

            // Init transfer on server
            val transferId = UUID.randomUUID().toString()
            if (!api.initTransfer(transferId, transfer.recipientDeviceId, result.encryptedMeta, result.encryptedChunks.size)) {
                throw Exception("Failed to init transfer on server")
            }

            // Upload chunks
            for ((index, chunk) in result.encryptedChunks.withIndex()) {
                Log.d(TAG, "Uploading chunk ${index + 1}/${result.encryptedChunks.size}")
                val chunkResult = api.uploadChunk(transferId, index, chunk)
                    ?: throw Exception("Failed to upload chunk $index")
                db.transferDao().updateProgress(transferDbId, index + 1, result.encryptedChunks.size)
            }

            db.transferDao().setTransferId(transferDbId, transferId)
            db.transferDao().updateStatus(transferDbId, TransferStatus.COMPLETE)
            AppLog.log("Upload", "Complete: ${transfer.displayName}")
            return Result.success()

        } catch (e: Exception) {
            Log.e(TAG, "Upload failed: ${e.message}", e)
            AppLog.log("Upload", "Failed: ${transfer.displayName} - ${e.message}")
            db.transferDao().updateStatus(transferDbId, TransferStatus.FAILED, e.message)
            return Result.retry()
        }
    }

}
