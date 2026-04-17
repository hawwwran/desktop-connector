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
import com.desktopconnector.network.FcmManager
import kotlinx.coroutines.*
import org.json.JSONObject
import java.util.Collections
import java.util.concurrent.ConcurrentHashMap

class PollService : Service() {

    companion object {
        private const val TAG = "PollService"
        private const val CHANNEL_ID = "dc_service_v3"
        private const val CHANNEL_TRANSFER = "dc_transfers"
        private const val NOTIFICATION_ID = 1
        private const val POLL_INTERVAL = 10_000L
        private const val DELIVERY_STALL_TIMEOUT_MS = 2 * 60 * 1000L

        // Shared state for UI — "active", "unavailable", "testing", "offline"
        @Volatile var longPollStatus: String = "offline"
        @Volatile var retryLongPoll: Boolean = false
        @Volatile var fcmWakeSignal: Boolean = false
        @Volatile var fasttrackWakeSignal: Boolean = false
        // Active ApiClient — used to cancel long poll from FcmService
        @Volatile var activeApi: ApiClient? = null

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
        scope.launch { deliveryTrackerLoop() }
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

            mgr.createNotificationChannel(NotificationChannel(
                "dc_find_phone", "Find My Phone",
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = "Alarm when phone is being located"
                setBypassDnd(true)
            })
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

            // One-time FCM initialization attempt (retried on pairing or app restart)
            if (!FcmManager.isInitialized && !FcmManager.initAttempted) {
                try { FcmManager.initialize(applicationContext, prefs) } catch (_: Exception) {}
            }

            // Check for retry signal from settings
            if (retryLongPoll) {
                retryLongPoll = false
                longPollAvailable = null
                AppLog.log("Poll", "Long poll retry requested")
            }

            val api = ApiClient(prefs.serverUrl!!, prefs.deviceId!!, prefs.authToken!!)
            activeApi = api
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

                // Process fasttrack messages before long poll (which blocks 25s)
                if (fasttrackWakeSignal || FindPhoneManager.isRinging) {
                    AppLog.log("Fasttrack", "Processing (wake=$fasttrackWakeSignal, ringing=${FindPhoneManager.isRinging})")
                    fasttrackWakeSignal = false
                    handleFasttrackMessages(api, keyManager)
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

                        checkDeliveryStatus(api)
                    } else {
                        longPollAvailable = null
                        longPollStatus = "unavailable"
                        AppLog.log("Poll", "Long poll failed, will re-test next cycle")
                        val transfers = api.getPendingTransfers()
                        for (t in transfers) {
                            if (!running) break
                            handleIncomingTransfer(t, api, keyManager, prefs)
                        }
                        checkDeliveryStatus(api)
                        if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
                    }
                } else {
                    val transfers = api.getPendingTransfers()
                    for (t in transfers) {
                        if (!running) break
                        handleIncomingTransfer(t, api, keyManager, prefs)
                    }
                    checkDeliveryStatus(api)
                    if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Poll failed: ${e.message}")
                AppLog.log("Poll", "Failed: ${e.message}")
                if (isConnected) {
                    isConnected = false
                    longPollAvailable = null
                    updateNotification(buildIdleNotification(false))
                }
                if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
            }

            if (!isScreenOn()) {
                if (FcmManager.isInitialized) {
                    // FCM available — pure wait for push wake or screen on.
                    // Server learns liveness on-demand via ping/pong, not heartbeats.
                    AppLog.log("Poll", "Screen off, waiting for FCM wake")
                    while (!isScreenOn() && !fcmWakeSignal && !fasttrackWakeSignal && running) {
                        delay(500)
                    }
                    if (fcmWakeSignal) {
                        fcmWakeSignal = false
                        longPollAvailable = null  // re-test after wake
                        AppLog.log("Poll", "FCM wake, polling")
                        continue
                    }
                    if (fasttrackWakeSignal) {
                        // Don't clear — let the fasttrack check at top of loop consume it
                        longPollAvailable = null  // re-test after wake
                        AppLog.log("Poll", "Fasttrack FCM wake")
                        continue
                    }
                } else {
                    // No FCM — pause until screen on
                    AppLog.log("Poll", "Screen off, pausing")
                    while (!isScreenOn() && running) {
                        delay(500)
                    }
                }
                if (running) {
                    longPollAvailable = null  // re-test after screen wake
                    AppLog.log("Poll", "Screen on, resuming")
                    continue
                }
            }
            if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
        }
    }

    private suspend fun handleFasttrackMessages(api: ApiClient, keyManager: KeyManager) {
        AppLog.log("Fasttrack", "Fetching pending messages...")
        val messages = api.fasttrackPending()
        if (messages.isEmpty()) {
            AppLog.log("Fasttrack", "No pending messages")
            return
        }

        AppLog.log("Fasttrack", "Processing ${messages.size} message(s)")

        for (msg in messages) {
            val messageId = msg.getInt("id")
            val senderId = msg.getString("sender_id")
            val encryptedDataB64 = msg.getString("encrypted_data")

            val paired = keyManager.getPairedDevice(senderId)
            if (paired == null) {
                AppLog.log("Fasttrack", "Message $messageId from unknown device ${senderId.take(12)}, skipping")
                api.fasttrackAck(messageId)
                continue
            }

            try {
                val symmetricKey = Base64.decode(paired.symmetricKeyB64, Base64.NO_WRAP)
                val encryptedBytes = Base64.decode(encryptedDataB64, Base64.NO_WRAP)
                val plainBytes = CryptoUtils.decryptBlob(encryptedBytes, symmetricKey)
                val payload = JSONObject(String(plainBytes))

                val fn = payload.optString("fn", "")
                AppLog.log("Fasttrack", "Message $messageId: fn=$fn, payload=$payload")

                when (fn) {
                    "find-phone" -> {
                        val action = payload.optString("action", "")
                        AppLog.log("Fasttrack", "Find-phone action=$action")
                        FindPhoneManager.handleCommand(
                            applicationContext, action, payload,
                            senderId, api, symmetricKey
                        )
                    }
                    else -> AppLog.log("Fasttrack", "Unknown fn: $fn")
                }
            } catch (e: Exception) {
                AppLog.log("Fasttrack", "Failed to process message $messageId: ${e.message}")
                Log.e(TAG, "Failed to process fasttrack message $messageId: ${e.message}")
            }

            api.fasttrackAck(messageId)
            AppLog.log("Fasttrack", "Acked message $messageId")
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

        // Decrypt metadata first so we can show filename in progress UI
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
        val mimeType = metaJson.optString("mime_type", "application/octet-stream")
        val baseNonceB64 = metaJson.getString("base_nonce")
        val baseNonce = Base64.decode(baseNonceB64, Base64.NO_WRAP)
        val isFnTransfer = fileName.startsWith(".fn.")

        Log.i(TAG, "Receiving transfer $transferId from ${senderId.take(12)} ($chunkCount chunks): $fileName")

        // Acquire wake + wifi locks to prevent Doze from throttling the download
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        val wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DesktopConnector:download")
        val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as android.net.wifi.WifiManager
        @Suppress("DEPRECATION")
        val wifiMode = if (Build.VERSION.SDK_INT >= 29) android.net.wifi.WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                        else android.net.wifi.WifiManager.WIFI_MODE_FULL_HIGH_PERF
        val wifiLock = wifiManager.createWifiLock(wifiMode, "DesktopConnector:download")
        wakeLock.acquire(2 * 60 * 1000L) // 2 min, refreshed per chunk
        wifiLock.acquire() // released in finally block

        try {
            handleIncomingTransferInner(
                transferId, senderId, encryptedMeta, chunkCount, symmetricKey,
                metaJson, fileName, mimeType, baseNonce, isFnTransfer, api, prefs, wakeLock
            )
        } finally {
            if (wakeLock.isHeld) wakeLock.release()
            if (wifiLock.isHeld) wifiLock.release()
        }
    }

    private suspend fun handleIncomingTransferInner(
        transferId: String, senderId: String, encryptedMeta: String, chunkCount: Int,
        symmetricKey: ByteArray, metaJson: JSONObject, fileName: String, mimeType: String,
        baseNonce: ByteArray, isFnTransfer: Boolean, api: ApiClient, prefs: AppPreferences,
        wakeLock: PowerManager.WakeLock,
    ) {
        val db = AppDatabase.getInstance(this)

        // Reuse existing DB row on retry (prevents duplicates after transient failures)
        var dbRowId: Long = 0
        if (!isFnTransfer) {
            val existing = db.transferDao().getByTransferId(transferId)
            if (existing != null) {
                dbRowId = existing.id
                db.transferDao().updateStatus(dbRowId, TransferStatus.UPLOADING)
                db.transferDao().updateProgress(dbRowId, 0, chunkCount)
                AppLog.log("Recv", "Resuming transfer $transferId (reusing row $dbRowId)")
            } else {
                dbRowId = db.transferDao().insert(QueuedTransfer(
                    contentUri = "",
                    displayName = fileName,
                    displayLabel = fileName,
                    mimeType = mimeType,
                    sizeBytes = 0,
                    recipientDeviceId = prefs.deviceId ?: "",
                    direction = TransferDirection.INCOMING,
                    status = TransferStatus.UPLOADING,
                    totalChunks = chunkCount,
                    chunksUploaded = 0,
                    transferId = transferId,
                ))
            }
        }

        // Download all chunks with progress updates and retry
        val chunks = mutableListOf<ByteArray>()
        for (i in 0 until chunkCount) {
            var chunk: ByteArray? = null
            for (attempt in 1..3) {
                chunk = api.downloadChunk(transferId, i)
                if (chunk != null) break
                AppLog.log("Recv", "Chunk $i failed (attempt $attempt/3), retrying...")
                delay(2000L * attempt)
            }
            if (chunk == null) {
                Log.e(TAG, "Failed to download chunk $i of $transferId after 3 attempts")
                if (dbRowId > 0) {
                    db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Download failed at chunk ${i + 1}/$chunkCount")
                }
                return
            }
            chunks.add(chunk)
            wakeLock.acquire(2 * 60 * 1000L)  // refresh: 2 min from last chunk
            if (dbRowId > 0) {
                // Check if user deleted the item — stop downloading if so
                if (db.transferDao().exists(dbRowId) == 0) {
                    AppLog.log("Recv", "Download cancelled by user at chunk ${i + 1}/$chunkCount")
                    api.ackTransfer(transferId)
                    return
                }
                db.transferDao().updateProgress(dbRowId, i + 1, chunkCount)
            }
        }

        // Decrypt chunks
        val plainParts = mutableListOf<ByteArray>()
        for ((i, chunk) in chunks.withIndex()) {
            try {
                plainParts.add(CryptoUtils.decryptBlob(chunk, symmetricKey))
            } catch (e: Exception) {
                Log.e(TAG, "Failed to decrypt chunk $i: ${e.message}")
                if (dbRowId > 0) {
                    db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Decryption failed")
                }
                return
            }
        }

        val data = plainParts.fold(ByteArray(0)) { acc, part -> acc + part }
        AppLog.log("Recv", "Decrypted: $fileName (${data.size} bytes)")

        // Handle .fn. transfers
        val displayLabel: String
        var savedUri = ""
        if (isFnTransfer) {
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

        // Finalize in history
        if (isFnTransfer) {
            if (fileName != ".fn.unpair") {
                db.transferDao().insert(QueuedTransfer(
                    contentUri = savedUri,
                    displayName = fileName,
                    displayLabel = displayLabel,
                    mimeType = mimeType,
                    sizeBytes = data.size.toLong(),
                    recipientDeviceId = prefs.deviceId ?: "",
                    direction = TransferDirection.INCOMING,
                    status = TransferStatus.COMPLETE,
                ))
                db.transferDao().trimHistory()
                showTransferNotification(displayLabel)
            }
        } else {
            // Check if user deleted the row while downloading — if so, skip notification
            val stillExists = db.transferDao().exists(dbRowId) > 0
            db.transferDao().completeDownload(
                dbRowId, TransferStatus.COMPLETE, savedUri, displayLabel, data.size.toLong()
            )
            db.transferDao().trimHistory()
            if (stillExists) {
                showTransferNotification(displayLabel)
            }
        }
    }

    /**
     * DeliveryTracker — paints per-chunk "Delivering X/Y" progress for OUTGOING
     * transfers while the desktop pulls them off the server.
     *
     * Cadence: 500ms tick, single in-flight poll at a time (overlap → skip + log),
     * 750ms abort timeout per poll. Idle when no active deliveries or screen off.
     *
     * Stall safeguard: if chunks_downloaded does not advance for 2 minutes on a
     * given transfer, the tracker gives up tracking that transfer (clears its
     * progress fields so UI falls back to "Sent"). The transfer row stays
     * COMPLETE/undelivered; the long-poll inline sent_status + app-restart
     * delivery check still catch eventual delivery if the desktop comes online.
     *
     * Does NOT mark delivered=true itself. When the server reports delivery_state
     * == "delivered" for any tracked transfer, the tracker clears its own
     * progress fields and delegates to checkDeliveryStatus — the same path that
     * runs on app start — as the single source of truth for the "Delivered" flag.
     */
    private val trackerLastProgress = ConcurrentHashMap<String, Pair<Int, Long>>()
    private val trackerGaveUp: MutableSet<String> =
        Collections.newSetFromMap(ConcurrentHashMap())

    private suspend fun deliveryTrackerLoop() {
        delay(3000)
        val prefs = AppPreferences(this)
        val db = AppDatabase.getInstance(this)
        var inFlightJob: Job? = null

        while (running) {
            val tickStart = System.currentTimeMillis()
            try {
                if (prefs.serverUrl == null || prefs.deviceId == null || prefs.authToken == null
                    || !isScreenOn()) {
                    delay(500); continue
                }

                val allActiveIds = db.transferDao().getActiveDeliveryIds().toSet()

                // Prune tracker state for transfers no longer active (deleted, delivered via long-poll, etc.)
                trackerGaveUp.retainAll(allActiveIds)
                trackerLastProgress.keys.retainAll(allActiveIds)

                val trackedIds = (allActiveIds - trackerGaveUp).toList()
                if (trackedIds.isEmpty()) {
                    delay(500); continue
                }

                if (inFlightJob?.isActive == true) {
                    AppLog.log("Delivery", "Skip tick — previous poll still in flight")
                } else {
                    val api = ApiClient(prefs.serverUrl!!, prefs.deviceId!!, prefs.authToken!!)
                    inFlightJob = scope.launch {
                        try {
                            withTimeout(750) { runDeliveryPoll(api, trackedIds, db) }
                        } catch (_: TimeoutCancellationException) {
                            AppLog.log("Delivery", "Poll timed out (>750ms), aborted")
                        } catch (_: Exception) {
                            // transient — next tick retries
                        }
                    }
                }
            } catch (_: Exception) {
                // loop must never die
            }
            val elapsed = System.currentTimeMillis() - tickStart
            delay((500 - elapsed).coerceAtLeast(0))
        }
    }

    private suspend fun runDeliveryPoll(
        api: ApiClient,
        activeIds: List<String>,
        db: AppDatabase,
    ) {
        val statuses = api.getSentStatus()
        val byId = statuses.associateBy { it.getString("transfer_id") }
        val now = System.currentTimeMillis()

        var anyJustDelivered = false
        for (tid in activeIds) {
            val s = byId[tid] ?: continue
            val state = s.optString("delivery_state", "not_started")
            val downloaded = s.optInt("chunks_downloaded", 0)
            val total = s.optInt("chunk_count", 0)

            if (state == "delivered") {
                db.transferDao().clearDeliveryProgress(tid)
                trackerLastProgress.remove(tid)
                anyJustDelivered = true
                continue
            }

            // Stall detection: timer resets when chunks_downloaded advances.
            // DB writes only on change — trackerLastProgress already tells us
            // whether the value moved since last tick.
            val prev = trackerLastProgress[tid]
            val advanced = prev == null || prev.first != downloaded

            if (advanced) {
                trackerLastProgress[tid] = Pair(downloaded, now)
                // not_started reports 0 chunks; keep bar visible at 0/N.
                val dbValue = if (state == "in_progress") downloaded else 0
                db.transferDao().updateDeliveryProgress(tid, dbValue, total)
            } else if (now - prev!!.second > DELIVERY_STALL_TIMEOUT_MS) {
                AppLog.log("Delivery", "Stall on $tid after ${(now - prev.second) / 1000}s — giving up")
                db.transferDao().clearDeliveryProgress(tid)
                trackerGaveUp.add(tid)
                trackerLastProgress.remove(tid)
            }
        }

        if (anyJustDelivered) {
            // Hand off to the standard sent-status path (same one used on app start).
            checkDeliveryStatus(api)
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
