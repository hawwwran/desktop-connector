package com.desktopconnector.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.os.PowerManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.util.Base64
import android.util.Log
import com.desktopconnector.R
import com.desktopconnector.MainActivity
import com.desktopconnector.crypto.CryptoUtils
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppDatabase
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.data.QueuedTransfer
import com.desktopconnector.data.TransferDirection
import com.desktopconnector.data.TransferStatus
import com.desktopconnector.network.ApiClient
import kotlinx.coroutines.*
import org.json.JSONObject

class PollService : Service() {

    companion object {
        private const val TAG = "PollService"
        private const val CHANNEL_ID = "dc_service_v3"
        private const val CHANNEL_TRANSFER = "dc_transfers"
        private const val NOTIFICATION_ID = 1
        private const val POLL_INTERVAL = 10_000L

        // Shared state for UI — "active", "unavailable", "testing", "offline"
        @Volatile var longPollStatus: String = "offline"
        @Volatile var retryLongPoll: Boolean = false

        fun start(context: Context) {
            val intent = Intent(context, PollService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var running = true

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannels()
        startForeground(NOTIFICATION_ID, buildIdleNotification(false))
        scope.launch { pollLoop() }
        Log.i(TAG, "PollService started")
    }

    override fun onDestroy() {
        running = false
        scope.cancel()
        Log.i(TAG, "PollService stopped")
        super.onDestroy()
    }

    private fun isScreenOn(): Boolean {
        val pm = getSystemService(PowerManager::class.java)
        return pm.isInteractive
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    private var isConnected = false

    private fun createNotificationChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = getSystemService(NotificationManager::class.java)

            // Remove old channels from previous versions
            for (old in listOf("dc_service", "dc_service_v2")) {
                mgr.deleteNotificationChannel(old)
            }

            mgr.createNotificationChannel(NotificationChannel(
                CHANNEL_ID, "Desktop Connector Service",
                NotificationManager.IMPORTANCE_MIN,
            ).apply {
                description = "Keeps the app running to receive transfers"
                setShowBadge(false)
            })

            mgr.createNotificationChannel(NotificationChannel(
                CHANNEL_TRANSFER, "Transfers",
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply { description = "Notifications for received transfers" })
        }
    }

    private fun buildIdleNotification(connected: Boolean): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pending = PendingIntent.getActivity(this, 0, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

        val icon = if (connected) R.drawable.ic_notif_connected else R.drawable.ic_notif_disconnected
        val statusText = if (connected) "Connected" else "Disconnected"

        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(icon)
            .setContentTitle("Desktop Connector")
            .setContentText(statusText)
            .setContentIntent(pending)
            .setOngoing(true)
            .build()
    }


    private fun updateNotification(notification: Notification) {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.notify(NOTIFICATION_ID, notification)
    }

    private suspend fun pollLoop() {
        delay(3000)

        val prefs = AppPreferences(this)
        var lastPollTime = 0L
        var longPollAvailable: Boolean? = null  // null = untested
        while (running) {
            if (prefs.serverUrl == null || prefs.deviceId == null || prefs.authToken == null) {
                delay(5000)
                continue
            }

            val keyManager = KeyManager(this)
            if (!keyManager.hasPairedDevice()) {
                delay(5000)
                continue
            }

            // Check for retry signal from settings
            if (retryLongPoll) {
                retryLongPoll = false
                longPollAvailable = null
                AppLog.log("Poll", "Long poll retry requested")
            }

            val api = ApiClient(prefs.serverUrl!!, prefs.deviceId!!, prefs.authToken!!)
            try {
                // Ensure we're connected first
                if (!isConnected) {
                    longPollStatus = "offline"
                    val reachable = api.healthCheck()
                    if (reachable) {
                        isConnected = true
                        longPollAvailable = null
                        AppLog.log("Poll", "Connected to ${prefs.serverUrl}")
                        updateNotification(buildIdleNotification(true))
                    } else {
                        delay(POLL_INTERVAL)
                        continue
                    }
                }

                // Quick test before committing to long poll
                if (longPollAvailable == null) {
                    longPollStatus = "testing"
                    val testResult = api.longPollNotify(0, test = true)
                    longPollAvailable = testResult != null
                    longPollStatus = if (longPollAvailable == true) "active" else "unavailable"
                    AppLog.log("Poll", "Long poll ${if (longPollAvailable == true) "available" else "not available"}")
                }

                if (longPollAvailable == true) {
                    val notifyResult = api.longPollNotify(lastPollTime / 1000)

                    if (notifyResult != null) {
                        lastPollTime = System.currentTimeMillis()

                        val hasPending = notifyResult.optBoolean("pending", false)
                        val hasDelivered = notifyResult.optBoolean("delivered", false)

                        if (hasPending) {
                            val transfers = api.getPendingTransfers()
                            if (transfers.isNotEmpty()) {
                                AppLog.log("Poll", "Found ${transfers.size} pending transfer(s)")
                            }
                            for (t in transfers) {
                                if (!running) break
                                handleIncomingTransfer(t, api, keyManager, prefs)
                            }
                        }

                        // Always check delivery — server can't reliably track "new" deliveries
                        checkDeliveryStatus(api)
                    } else {
                        longPollAvailable = false
                        longPollStatus = "unavailable"
                        AppLog.log("Poll", "Long poll not available, using regular polling")
                        // Do a regular poll + sleep
                        val transfers = api.getPendingTransfers()
                        for (t in transfers) {
                            if (!running) break
                            handleIncomingTransfer(t, api, keyManager, prefs)
                        }
                        checkDeliveryStatus(api)
                        delay(POLL_INTERVAL)
                    }
                } else {
                    // Regular polling — long poll not available
                    val transfers = api.getPendingTransfers()
                    for (t in transfers) {
                        if (!running) break
                        handleIncomingTransfer(t, api, keyManager, prefs)
                    }
                    checkDeliveryStatus(api)
                    delay(POLL_INTERVAL)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Poll failed: ${e.message}")
                AppLog.log("Poll", "Failed: ${e.message}")
                if (isConnected) {
                    isConnected = false
                    longPollAvailable = null
                    updateNotification(buildIdleNotification(false))
                }
                delay(POLL_INTERVAL)
            }

            if (!isScreenOn()) {
                AppLog.log("Poll", "Screen off, pausing")
                while (!isScreenOn() && running) {
                    delay(500)
                }
                if (running) {
                    AppLog.log("Poll", "Screen on, resuming")
                    continue
                }
            }
            delay(POLL_INTERVAL)
        }
    }

    private suspend fun handleIncomingTransfer(
        transfer: JSONObject,
        api: ApiClient,
        keyManager: KeyManager,
        prefs: AppPreferences,
    ) {
        val transferId = transfer.getString("transfer_id")
        val senderId = transfer.getString("sender_id")
        val encryptedMeta = transfer.getString("encrypted_meta")
        val chunkCount = transfer.getInt("chunk_count")

        val paired = keyManager.getPairedDevice(senderId)
        if (paired == null) {
            Log.w(TAG, "Transfer from unknown device $senderId, skipping")
            return
        }

        val symmetricKey = Base64.decode(paired.symmetricKeyB64, Base64.NO_WRAP)

        Log.i(TAG, "Receiving transfer $transferId from ${senderId.take(12)} ($chunkCount chunks)")


        // Download all chunks
        val chunks = mutableListOf<ByteArray>()
        for (i in 0 until chunkCount) {
            val chunk = api.downloadChunk(transferId, i)
            if (chunk == null) {
                Log.e(TAG, "Failed to download chunk $i of $transferId")
                return
            }
            chunks.add(chunk)
        }

        // Decrypt metadata
        val metaBlob = Base64.decode(encryptedMeta, Base64.NO_WRAP)
        val metaJson: JSONObject
        try {
            val metaBytes = CryptoUtils.decryptBlob(metaBlob, symmetricKey)
            metaJson = JSONObject(String(metaBytes))
        } catch (e: Exception) {
            Log.e(TAG, "Failed to decrypt metadata: ${e.message}")
            return
        }

        val fileName = metaJson.getString("filename")
        val baseNonceB64 = metaJson.getString("base_nonce")
        val baseNonce = Base64.decode(baseNonceB64, Base64.NO_WRAP)

        // Decrypt chunks
        val plainParts = mutableListOf<ByteArray>()
        for ((i, chunk) in chunks.withIndex()) {
            try {
                val nonce = CryptoUtils.makeChunkNonce(baseNonce, i)
                // chunk has nonce prepended from encrypt_blob, so use decryptBlob
                plainParts.add(CryptoUtils.decryptBlob(chunk, symmetricKey))
            } catch (e: Exception) {
                Log.e(TAG, "Failed to decrypt chunk $i: ${e.message}")
                return
            }
        }

        val data = plainParts.fold(ByteArray(0)) { acc, part -> acc + part }
        AppLog.log("Recv", "Decrypted: $fileName (${data.size} bytes)")

        // Handle .fn. transfers
        val displayLabel: String
        var savedUri = ""
        if (fileName.startsWith(".fn.")) {
            displayLabel = handleFnTransfer(fileName, data)
            AppLog.log("Recv", "Fn transfer: $fileName -> $displayLabel")
        } else {
            displayLabel = fileName
            val savedFile = saveFile(fileName, data)
            if (savedFile != null) {
                savedUri = Uri.fromFile(savedFile).toString()
            }
            AppLog.log("Recv", "Saved file: $fileName")
        }

        api.ackTransfer(transferId)
        AppLog.log("Recv", "Acked $transferId")

        // Record in history (skip system .fn. commands like unpair)
        if (fileName != ".fn.unpair") {
            val db = AppDatabase.getInstance(this)
            db.transferDao().insert(QueuedTransfer(
                contentUri = savedUri,
                displayName = fileName,
                displayLabel = displayLabel,
                mimeType = metaJson.optString("mime_type", "application/octet-stream"),
                sizeBytes = data.size.toLong(),
                recipientDeviceId = prefs.deviceId ?: "",
                direction = TransferDirection.INCOMING,
                status = TransferStatus.COMPLETE,
            ))
            db.transferDao().trimHistory()
            showTransferNotification(displayLabel)
        }
    }

    private suspend fun checkDeliveryStatus(api: ApiClient) {
        val db = AppDatabase.getInstance(this)
        val undelivered = db.transferDao().getUndeliveredTransferIds()
        if (undelivered.isEmpty()) return

        val statuses = api.getSentStatus()
        val deliveredIds = statuses
            .filter { it.optString("status") == "delivered" }
            .map { it.getString("transfer_id") }
            .toSet()

        for (tid in undelivered) {
            if (tid in deliveredIds) {
                db.transferDao().markDelivered(tid)
                Log.i(TAG, "Transfer $tid delivered")
            }
        }
    }

    private fun handleFnTransfer(fileName: String, data: ByteArray): String {
        val parts = fileName.split(".")  // ["", "fn", "clipboard", "text"]
        if (parts.size < 3) return fileName

        val fn = parts[2]
        if (fn == "clipboard") {
            val subtype = if (parts.size > 3) parts[3] else "text"
            return when (subtype) {
                "text" -> {
                    val text = String(data)
                    pushTextToClipboard(text)
                    val hasUrl = com.desktopconnector.ui.containsSingleUrl(text)
                    if (hasUrl) text else if (text.length > 40) text.take(40) + "..." else text
                }
                "image" -> {
                    pushImageToClipboard(data)
                    "Clipboard image"
                }
                else -> fileName
            }
        } else if (fn == "unpair") {
            Log.i(TAG, "Received unpair request from desktop")
            val keyManager = KeyManager(this)
            keyManager.getFirstPairedDevice()?.let {
                keyManager.removePairedDevice(it.deviceId)
            }
            showTransferNotification("Desktop disconnected")
            return "Unpaired by desktop"
        }
        return fileName
    }

    private fun pushTextToClipboard(text: String) {
        val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        val clip = ClipData.newPlainText("Desktop Connector", text)
        clipboard.setPrimaryClip(clip)
        Log.i(TAG, "Text pushed to clipboard (${text.length} chars)")
    }

    private fun pushImageToClipboard(data: ByteArray) {
        // Save to cache, then set clipboard URI
        try {
            val file = java.io.File(cacheDir, "clipboard_received.png")
            file.writeBytes(data)
            val uri = androidx.core.content.FileProvider.getUriForFile(
                this, "$packageName.fileprovider", file
            )
            val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
            val clip = ClipData.newUri(contentResolver, "Clipboard image", uri)
            clipboard.setPrimaryClip(clip)
            Log.i(TAG, "Image pushed to clipboard")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to push image to clipboard: ${e.message}")
            // Fallback: just save the file
            saveFile("clipboard_image.png", data)
        }
    }

    private fun saveFile(fileName: String, data: ByteArray): java.io.File? {
        return try {
            val dir = java.io.File(android.os.Environment.getExternalStorageDirectory(), "DesktopConnector")
            dir.mkdirs()
            var target = java.io.File(dir, fileName)
            var counter = 1
            while (target.exists()) {
                val stem = fileName.substringBeforeLast(".")
                val ext = fileName.substringAfterLast(".", "")
                target = java.io.File(dir, "${stem}_${counter}${if (ext.isNotEmpty()) ".$ext" else ""}")
                counter++
            }
            target.writeBytes(data)
            Log.i(TAG, "File saved: ${target.absolutePath}")

            // Notify MediaStore so the file appears in gallery/pickers
            android.media.MediaScannerConnection.scanFile(
                this, arrayOf(target.absolutePath), null, null
            )

            target
        } catch (e: Exception) {
            Log.e(TAG, "Failed to save file: ${e.message}")
            null
        }
    }

    private fun showTransferNotification(label: String) {
        val mgr = getSystemService(NotificationManager::class.java)
        val intent = Intent(this, MainActivity::class.java)
        val pending = PendingIntent.getActivity(this, 0, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

        val notification = Notification.Builder(this, CHANNEL_TRANSFER)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("Received")
            .setContentText(label)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()

        mgr.notify(System.currentTimeMillis().toInt(), notification)
    }
}
