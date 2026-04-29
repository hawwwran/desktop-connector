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
import android.content.pm.ServiceInfo
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
import com.desktopconnector.network.ApiClient.ChunkDownloadResult
import com.desktopconnector.network.AuthObservation
import com.desktopconnector.network.FcmManager
import com.desktopconnector.network.StreamReceiveOutcome
import com.desktopconnector.network.downloadStreamLoop
import com.desktopconnector.messaging.DeviceMessage
import com.desktopconnector.messaging.MessageAdapters
import com.desktopconnector.messaging.MessageDispatcher
import com.desktopconnector.messaging.MessageType
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
        private const val STALE_PART_TTL_MS = 60 * 60 * 1000L
        private val SAFE_TRANSFER_ID = Regex("^[a-zA-Z0-9-]+$")
        // Brand accent (DcBlue700 = #2058F0) — tints notifications in the shade header.
        private val BRAND_ACCENT = android.graphics.Color.rgb(0x20, 0x58, 0xF0)

        // Shared state for UI — "active", "unavailable", "testing", "offline"
        @Volatile var longPollStatus: String = "offline"
        @Volatile var retryLongPoll: Boolean = false
        @Volatile var fcmWakeSignal: Boolean = false
        @Volatile var fasttrackWakeSignal: Boolean = false
        // Active ApiClient — used to cancel long poll from FcmService
        @Volatile var activeApi: ApiClient? = null
        // Active service instance — used by FindPhoneManager to toggle FGS location type
        @Volatile var activeService: PollService? = null

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
    private val messageDispatcher = MessageDispatcher().apply {
        register(MessageType.CLIPBOARD_TEXT) { msg ->
            val textPayload = msg.payload["text"] as? String ?: return@register
            pushTextToClipboard(textPayload)
        }
        register(MessageType.PAIRING_UNPAIR) { msg ->
            val senderId = msg.senderId
            if (senderId == null) {
                AppLog.log("Pairing", "pairing.unpair.received error_kind=missing_sender_id")
                return@register
            }
            val keyManager = KeyManager(this@PollService)
            val name = keyManager.getPairedDevice(senderId)?.name ?: "desktop"
            AppLog.log("Pairing", "pairing.unpair.received peer=${senderId.take(12)}")
            scope.launch {
                AppDatabase.getInstance(this@PollService).transferDao()
                    .deleteAllForPeer(senderId)
                com.desktopconnector.crypto.PairingRepository
                    .getInstance(this@PollService).unpair(senderId)
            }
            showTransferNotification("Disconnected from $name")
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        activeService = this
        createNotificationChannels()
        // API 29+: assert only DATA_SYNC at startup. LOCATION is declared in the manifest
        // and upgraded into at find-phone start (see setForegroundType); asserting it here
        // would crash on any fresh install that has not yet granted location permission.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                buildIdleNotification(false),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(NOTIFICATION_ID, buildIdleNotification(false))
        }
        scope.launch { pollLoop() }
        scope.launch { deliveryTrackerLoop() }
        scope.launch { sweepStaleParts() }
        scope.launch { observeAuthForNotification() }
        Log.i(TAG, "PollService started")
    }

    /** Keep the persistent notification honest about auth state: a 401/403
     *  on any poll call flips it to Disconnected immediately; a 2xx
     *  authenticated response restores it. Without this the notification
     *  would cling to "Connected" because `healthCheck()` used to hit an
     *  optional-auth endpoint and ordinary poll calls silently dropped
     *  auth failures. */
    private suspend fun observeAuthForNotification() {
        ApiClient.authObservations.collect { obs ->
            when (obs) {
                is AuthObservation.Failure -> {
                    if (isConnected) {
                        isConnected = false
                        updateNotification(buildIdleNotification(false))
                        AppLog.log("Auth",
                            "notification.auth.disconnected kind=${obs.kind.name}",
                            "warning")
                    }
                }
                is AuthObservation.Success -> {
                    if (!isConnected) {
                        isConnected = true
                        updateNotification(buildIdleNotification(true))
                        AppLog.log("Auth", "notification.auth.connected")
                    }
                }
            }
        }
    }

    /**
     * Delete orphaned `.incoming_*.part` files in DesktopConnector/.parts/
     * that were left behind by previously aborted receives (force-stop,
     * OOM kill, reboot). Runs once on service start.
     */
    private fun sweepStaleParts() {
        try {
            val partsDir = java.io.File(
                android.os.Environment.getExternalStorageDirectory(),
                "DesktopConnector/.parts"
            )
            if (!partsDir.isDirectory) return
            val cutoff = System.currentTimeMillis() - STALE_PART_TTL_MS
            val removed = partsDir.listFiles()
                ?.filter { it.name.startsWith(".incoming_") && it.lastModified() < cutoff }
                ?.count { it.delete() } ?: 0
            if (removed > 0) AppLog.log("Recv", "Cleaned up $removed stale .part file(s)")
        } catch (e: Exception) {
            Log.w(TAG, "Parts sweep failed: ${e.message}")
        }
    }

    override fun onDestroy() {
        running = false
        scope.cancel()
        if (activeService === this) activeService = null
        Log.i(TAG, "PollService stopped")
        super.onDestroy()
    }

    /**
     * Re-assert the foreground-service type. Called by FindPhoneManager to add
     * LOCATION while the alarm is active (so GPS survives Doze) and to drop it
     * again when the alarm stops. Caller must hold ACCESS_{FINE,COARSE}_LOCATION
     * before requesting includeLocation=true, or startForeground will throw.
     */
    fun setForegroundType(includeLocation: Boolean) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        val type = if (includeLocation) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC or
                ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        } else {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        }
        try {
            startForeground(NOTIFICATION_ID, buildIdleNotification(isConnected), type)
            AppLog.log("Poll", "fgs.type.changed location=$includeLocation")
        } catch (e: SecurityException) {
            AppLog.log("Poll", "fgs.type.denied reason=${e.message}", "warning")
        }
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

        // Title-only, no large icon: Android 12+ already surfaces the app name and icon
        // in the header row, so repeating "Desktop Connector" and attaching a brand bitmap
        // just inflates the notification. Single-line status keeps it compact.
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(icon)
            .setColor(BRAND_ACCENT)
            .setContentTitle(statusText)
            .setContentIntent(pending)
            .setOngoing(true)
            .build()
    }


    private fun markDisconnected(reason: String) {
        if (!isConnected) return
        isConnected = false
        longPollStatus = "offline"
        updateNotification(buildIdleNotification(false))
        AppLog.log("Poll", "connection.lost.detected reason=$reason", "warning")
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

            val pairingRepo = com.desktopconnector.crypto.PairingRepository.getInstance(this)
            if (pairingRepo.pairs.value.isEmpty()) {
                delay(5000)
                continue
            }
            val keyManager = KeyManager(this)

            // One-time FCM initialization attempt (retried on pairing or app restart)
            if (!FcmManager.isInitialized && !FcmManager.initAttempted) {
                try { FcmManager.initialize(applicationContext, prefs) } catch (_: Exception) {}
            }

            // Check for retry signal from settings
            if (retryLongPoll) {
                retryLongPoll = false
                longPollAvailable = null
                AppLog.log("Poll", "poll.notify.retry_requested")
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
                        AppLog.log("Poll", "connection.check.succeeded")
                        updateNotification(buildIdleNotification(true))
                    } else {
                        delay(POLL_INTERVAL)
                        continue
                    }
                }

                // Process fasttrack messages before long poll (which blocks 25s)
                if (fasttrackWakeSignal || FindPhoneManager.isRinging) {
                    AppLog.log("Fasttrack", "fasttrack.message.processing wake=$fasttrackWakeSignal ringing=${FindPhoneManager.isRinging}", "debug")
                    fasttrackWakeSignal = false
                    handleFasttrackMessages(api, keyManager)
                }

                // Quick test before committing to long poll
                if (longPollAvailable == null) {
                    longPollStatus = "testing"
                    val testResult = api.longPollNotify(0, test = true)
                    if (testResult == null && !api.healthCheck()) {
                        markDisconnected("probe_null")
                        if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
                        continue
                    }
                    longPollAvailable = testResult != null
                    longPollStatus = if (longPollAvailable == true) "active" else "unavailable"
                    if (longPollAvailable == true) {
                        AppLog.log("Poll", "poll.notify.available")
                    } else {
                        AppLog.log("Poll", "poll.notify.unavailable", "warning")
                    }
                }

                if (longPollAvailable == true) {
                    val notifyResult = api.longPollNotify(lastPollTime / 1000)

                    if (notifyResult != null) {
                        lastPollTime = System.currentTimeMillis()

                        val hasPending = notifyResult.optBoolean("pending", false)

                        if (hasPending) {
                            val transfers = api.getPendingTransfers()
                            if (transfers.isNotEmpty()) {
                                AppLog.log("Poll", "transfer.pending.found count=${transfers.size}")
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
                        AppLog.log("Poll", "poll.notify.failed reason=will_re_test_next_cycle", "warning")
                        if (!api.healthCheck()) {
                            markDisconnected("long_poll_null")
                            if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
                            continue
                        }
                        val transfers = api.getPendingTransfers()
                        for (t in transfers) {
                            if (!running) break
                            handleIncomingTransfer(t, api, keyManager, prefs)
                        }
                        checkDeliveryStatus(api)
                        if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
                    }
                } else {
                    if (!api.healthCheck()) {
                        markDisconnected("poll_fallback")
                        if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
                        continue
                    }
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
                AppLog.log("Poll", "connection.check.failed error_kind=${e.javaClass.simpleName}", "warning")
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
                    AppLog.log("Poll", "poll.loop.screen_off reason=waiting_fcm")
                    while (!isScreenOn() && !fcmWakeSignal && !fasttrackWakeSignal && running) {
                        delay(500)
                    }
                    if (fcmWakeSignal) {
                        fcmWakeSignal = false
                        longPollAvailable = null  // re-test after wake
                        AppLog.log("Poll", "poll.loop.fcm_wake type=transfer")
                        continue
                    }
                    if (fasttrackWakeSignal) {
                        // Don't clear — let the fasttrack check at top of loop consume it
                        longPollAvailable = null  // re-test after wake
                        AppLog.log("Poll", "poll.loop.fcm_wake type=fasttrack")
                        continue
                    }
                } else {
                    // No FCM — pause until screen on
                    AppLog.log("Poll", "poll.loop.screen_off reason=paused")
                    while (!isScreenOn() && running) {
                        delay(500)
                    }
                }
                if (running) {
                    longPollAvailable = null  // re-test after screen wake
                    AppLog.log("Poll", "poll.loop.screen_on")
                    continue
                }
            }
            if (!fasttrackWakeSignal) delay(POLL_INTERVAL)
        }
    }

    private suspend fun handleFasttrackMessages(api: ApiClient, keyManager: KeyManager) {
        AppLog.log("Fasttrack", "fasttrack.message.pending_fetching", "debug")
        val messages = api.fasttrackPending()
        if (messages.isEmpty()) {
            // count=0 → skip emission (noise reduction)
            return
        }

        AppLog.log("Fasttrack", "fasttrack.message.pending_listed count=${messages.size}")

        for (msg in messages) {
            val messageId = msg.getInt("id")
            val senderId = msg.getString("sender_id")
            val encryptedDataB64 = msg.getString("encrypted_data")

            val paired = keyManager.getPairedDevice(senderId)
            if (paired == null) {
                AppLog.log("Fasttrack", "fasttrack.message.skipped message_id=$messageId sender=${senderId.take(12)} reason=unknown_device", "warning")
                api.fasttrackAck(messageId)
                continue
            }

            try {
                val symmetricKey = Base64.decode(paired.symmetricKeyB64, Base64.NO_WRAP)
                val encryptedBytes = Base64.decode(encryptedDataB64, Base64.NO_WRAP)
                val plainBytes = CryptoUtils.decryptBlob(encryptedBytes, symmetricKey)
                val payload = JSONObject(String(plainBytes))
                val message = MessageAdapters.fromFasttrackPayload(payload, senderId = senderId)
                val fn = payload.optString("fn", "")
                AppLog.log("Fasttrack", "fasttrack.message.processed message_id=$messageId fn=$fn")

                if (message == null) {
                    AppLog.log("Fasttrack", "fasttrack.command.unknown fn=$fn", "warning")
                } else {
                    when (message.type) {
                        MessageType.FIND_PHONE_START,
                        MessageType.FIND_PHONE_STOP,
                        MessageType.FIND_PHONE_LOCATION_UPDATE -> {
                            val action = payload.optString("action", "")
                            AppLog.log("Fasttrack", "fasttrack.command.received fn=find-phone action=$action")
                            FindPhoneManager.handleDeviceMessage(
                                applicationContext,
                                message,
                                api,
                                symmetricKey,
                            )
                        }
                        else -> {
                            if (!messageDispatcher.dispatch(message)) {
                                AppLog.log("Fasttrack", "fasttrack.command.unhandled type=${message.type}", "warning")
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                AppLog.log("Fasttrack", "fasttrack.message.processing_failed message_id=$messageId error_kind=${e.javaClass.simpleName}", "error")
                Log.e(TAG, "Failed to process fasttrack message $messageId: ${e.message}")
            }

            api.fasttrackAck(messageId)
            AppLog.log("Fasttrack", "fasttrack.message.acked message_id=$messageId", "debug")
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
        // Phase A server surfaces the negotiated mode on the pending-list
        // row. Absent / unknown => classic (this is how the field is
        // missing for pre-streaming clients or for servers that don't yet
        // emit it). `.fn.*` transfers always run classic regardless.
        val mode = transfer.optString("mode", "classic")

        // Defense-in-depth: transferId is used in a filename, so reject anything
        // that isn't an alphanumeric/UUID-shape id before touching the disk.
        if (!SAFE_TRANSFER_ID.matches(transferId)) {
            Log.w(TAG, "Rejecting transfer with unsafe id: ${transferId.take(40)}")
            return
        }

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
                metaJson, fileName, mimeType, baseNonce, isFnTransfer, mode,
                api, prefs, wakeLock
            )
        } finally {
            if (wakeLock.isHeld) wakeLock.release()
            if (wifiLock.isHeld) wifiLock.release()
        }
    }

    private suspend fun handleIncomingTransferInner(
        transferId: String, senderId: String, encryptedMeta: String, chunkCount: Int,
        symmetricKey: ByteArray, metaJson: JSONObject, fileName: String, mimeType: String,
        baseNonce: ByteArray, isFnTransfer: Boolean, mode: String,
        api: ApiClient, prefs: AppPreferences, wakeLock: PowerManager.WakeLock,
    ) {
        val db = AppDatabase.getInstance(this)
        if (isFnTransfer) {
            // `.fn.*` command transfers always classic — too small to benefit
            // from streaming, extra round-trips hurt. Plan §9 non-goal.
            receiveFnTransfer(transferId, senderId, chunkCount, symmetricKey, fileName, mimeType, api, prefs, db)
        } else if (mode == "streaming") {
            receiveStreamingTransfer(transferId, senderId, chunkCount, symmetricKey, fileName, mimeType, api, prefs, db, wakeLock)
        } else {
            receiveFileTransfer(transferId, senderId, chunkCount, symmetricKey, fileName, mimeType, api, prefs, db, wakeLock)
        }
    }

    /**
     * Receive a `.fn.*` command transfer. Payloads are tiny by design
     * (clipboard text, unpair signal, small clipboard images), so the tiny
     * in-memory path is kept for them — streaming to disk would be overkill.
     */
    private suspend fun receiveFnTransfer(
        transferId: String, senderId: String, chunkCount: Int, symmetricKey: ByteArray,
        fileName: String, mimeType: String, api: ApiClient, prefs: AppPreferences, db: AppDatabase,
    ) {
        val parts = ArrayList<ByteArray>(chunkCount)
        for (i in 0 until chunkCount) {
            val plain = downloadAndDecryptChunk(api, transferId, i, symmetricKey) ?: run {
                Log.w(TAG, ".fn transfer $transferId aborted: chunk $i unrecoverable")
                return
            }
            parts.add(plain)
        }
        val data = parts.fold(ByteArray(0)) { acc, part -> acc + part }
        AppLog.log("Recv", "fasttrack.command.received fn=$fileName bytes=${data.size}")

        if (fileName.startsWith(".fn.clipboard.image")) {
            val displayLabel = saveClipboardImageTransfer(data, mimeType, transferId, senderId, prefs, db)
                ?: return
            AppLog.log("Recv", "fasttrack.command.handled fn=$fileName label=$displayLabel")
            ackHandledTransfer(api, transferId)
            return
        }

        val displayLabel = handleFnTransfer(fileName, data, senderId = senderId)
        AppLog.log("Recv", "fasttrack.command.handled fn=$fileName label=$displayLabel")

        ackHandledTransfer(api, transferId)

        if (fileName != ".fn.unpair") {
            db.transferDao().insert(QueuedTransfer(
                contentUri = "",
                displayName = fileName,
                displayLabel = displayLabel,
                mimeType = mimeType,
                sizeBytes = data.size.toLong(),
                peerDeviceId = senderId,
                direction = TransferDirection.INCOMING,
                status = TransferStatus.COMPLETE,
            ))
            db.transferDao().trimHistory()
            showTransferNotification(displayLabel)
        }
    }

    private fun ackHandledTransfer(api: ApiClient, transferId: String) {
        try {
            if (api.ackTransfer(transferId))
                AppLog.log("Recv", "delivery.acked transfer_id=${transferId.take(12)}")
            else
                AppLog.log("Recv", "delivery.acked transfer_id=${transferId.take(12)} reason=already_executed")
        } catch (e: Exception) {
            AppLog.log("Recv", "delivery.acked transfer_id=${transferId.take(12)} error_kind=${e.javaClass.simpleName}")
        }
    }

    /**
     * Receive a normal file transfer by streaming chunks to a temp file on
     * disk. Memory usage is bounded by chunk size (2 MB) regardless of the
     * total file size. The temp file lives in the same directory as the
     * final destination so the finalize-rename is atomic.
     */
    private suspend fun receiveFileTransfer(
        transferId: String, senderId: String, chunkCount: Int, symmetricKey: ByteArray,
        fileName: String, mimeType: String, api: ApiClient, prefs: AppPreferences,
        db: AppDatabase, wakeLock: PowerManager.WakeLock,
    ) {
        // Reuse existing DB row on retry (prevents duplicates after transient failures)
        val existing = db.transferDao().getByTransferId(transferId)
        val dbRowId: Long = if (existing != null) {
            db.transferDao().updateStatus(existing.id, TransferStatus.UPLOADING)
            db.transferDao().updateProgress(existing.id, 0, chunkCount)
            AppLog.log("Recv", "transfer.download.resumed transfer_id=${transferId.take(12)}")
            existing.id
        } else {
            db.transferDao().insert(QueuedTransfer(
                contentUri = "",
                displayName = fileName,
                displayLabel = fileName,
                mimeType = mimeType,
                sizeBytes = 0,
                peerDeviceId = senderId,
                direction = TransferDirection.INCOMING,
                status = TransferStatus.UPLOADING,
                totalChunks = chunkCount,
                chunksUploaded = 0,
                transferId = transferId,
            ))
        }

        val dir = java.io.File(android.os.Environment.getExternalStorageDirectory(), "DesktopConnector")
        if (!dir.exists() && !dir.mkdirs()) {
            Log.e(TAG, "Could not create DesktopConnector directory")
            db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Cannot create download folder")
            return
        }
        val partsDir = java.io.File(dir, ".parts")
        if (!partsDir.exists() && !partsDir.mkdirs()) {
            Log.e(TAG, "Could not create DesktopConnector/.parts directory")
            db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Cannot create parts folder")
            return
        }
        val finalFile = pickUniqueTarget(dir, fileName)
        val tempFile = java.io.File(partsDir, ".incoming_${transferId}.part")
        tempFile.delete()  // clear stale partial from a prior aborted run

        var cancelled = false
        var failed = false
        try {
            java.io.BufferedOutputStream(java.io.FileOutputStream(tempFile)).use { out ->
                for (i in 0 until chunkCount) {
                    val plaintext = downloadAndDecryptChunk(api, transferId, i, symmetricKey)
                    if (plaintext == null) {
                        Log.e(TAG, "Chunk $i of $transferId unrecoverable (download or decrypt)")
                        failed = true
                        db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Chunk ${i + 1}/$chunkCount failed")
                        return
                    }
                    out.write(plaintext)

                    wakeLock.acquire(2 * 60 * 1000L)  // refresh: 2 min from last chunk

                    if (db.transferDao().exists(dbRowId) == 0) {
                        AppLog.log("Recv", "transfer.download.cancelled transfer_id=${transferId.take(12)} chunk_index=${i + 1}/$chunkCount")
                        cancelled = true
                        api.ackTransfer(transferId)
                        return
                    }
                    db.transferDao().updateProgress(dbRowId, i + 1, chunkCount)
                }
                out.flush()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Streaming write failed: ${e.message}", e)
            failed = true
            db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Write failed: ${e.message}")
            return
        } finally {
            if (cancelled || failed) {
                tempFile.delete()
            }
        }

        // Atomic finalize: rename temp → final within the same directory.
        if (!tempFile.renameTo(finalFile)) {
            Log.e(TAG, "Rename temp → final failed for $transferId")
            tempFile.delete()
            db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Cannot finalize download")
            return
        }
        val finalSize = finalFile.length()
        AppLog.log("Recv", "transfer.download.completed transfer_id=${transferId.take(12)} bytes=$finalSize name=${finalFile.name}")

        // Notify MediaStore so the file appears in gallery/pickers
        android.media.MediaScannerConnection.scanFile(
            this, arrayOf(finalFile.absolutePath), null, null
        )

        // ACK-after-durable-write: once the file is on disk under its final
        // name, the receive is locally successful. If ACK fails here the
        // sender will eventually stop seeing "delivering", but we must NOT
        // delete a fully received file just because the network hiccupped
        // before we could tell the server.
        val ackOk = try {
            api.ackTransfer(transferId)
        } catch (e: Exception) {
            Log.w(TAG, "ACK threw after durable write of $transferId: ${e.message}")
            false
        }
        if (ackOk) {
            AppLog.log("Recv", "delivery.acked transfer_id=${transferId.take(12)}")
        } else {
            AppLog.log("Recv", "delivery.acked transfer_id=${transferId.take(12)} reason=keeping_file_after_ack_failure")
        }

        val stillExists = db.transferDao().exists(dbRowId) > 0
        db.transferDao().completeDownload(
            dbRowId, TransferStatus.COMPLETE, Uri.fromFile(finalFile).toString(), fileName, finalSize
        )
        db.transferDao().trimHistory()
        if (stillExists) {
            showTransferNotification(fileName)
        }
    }

    /**
     * Streaming recipient loop (D.3).
     *
     * Mirrors `receiveFileTransfer` structurally — same `.incoming_<tid>.part`
     * file, same atomic rename, same wake + wifi lock ownership, same
     * cancellation-via-row-deletion semantics — but uses the typed
     * `downloadChunkTyped` path to distinguish 425 / 410 / network errors,
     * per-chunk ACKs each chunk after durable write, and honours the
     * streaming retry budgets per plan §10:
     *   - 5 min of continuous `TooEarly` (no progress) → recipient aborts
     *     with `recipient_abort`; row ends as FAILED(`stall_timeout`).
     *   - 3 network errors in a row (total ~12 s budget with the 2 s/retry
     *     ramp) → recipient aborts; row ends as FAILED(`network`).
     *   - 410 on a chunk GET → sender aborted upstream; row ends as ABORTED
     *     with the server-supplied reason.
     *   - Row deleted from the Room DB mid-loop (user swipe-delete) → fire
     *     `abortTransfer("recipient_abort")` so the sender learns; row
     *     already gone locally.
     *
     * After the final chunk's ACK, NO transfer-level `ackTransfer` is sent
     * — per-chunk ACKs already finalised delivery server-side (the server's
     * last-chunk ACK handler flips `downloaded=1`).
     *
     * Durability contract: every chunk is fsync'd (`FileDescriptor.sync`)
     * before we ack. That way if the process dies between ack and the next
     * chunk, the bytes we told the server we had are actually on disk.
     * Server has already deleted its copy once it saw the ack, so this
     * matters.
     */
    private suspend fun receiveStreamingTransfer(
        transferId: String, senderId: String, chunkCount: Int, symmetricKey: ByteArray,
        fileName: String, mimeType: String, api: ApiClient, prefs: AppPreferences,
        db: AppDatabase, wakeLock: PowerManager.WakeLock,
    ) {
        // Row handling — same "resume existing or insert new" pattern as
        // classic. Tagged mode=streaming so the tracker / UI know what
        // they're looking at. D.4b will own the sender-side
        // negotiatedMode stamp; here on the recipient side we just
        // record what we observed on the pending-list row.
        val existing = db.transferDao().getByTransferId(transferId)
        val dbRowId: Long = if (existing != null) {
            db.transferDao().updateStatus(existing.id, TransferStatus.UPLOADING)
            db.transferDao().updateProgress(existing.id, 0, chunkCount)
            AppLog.log("Recv", "transfer.download.resumed transfer_id=${transferId.take(12)} mode=streaming")
            existing.id
        } else {
            db.transferDao().insert(QueuedTransfer(
                contentUri = "",
                displayName = fileName,
                displayLabel = fileName,
                mimeType = mimeType,
                sizeBytes = 0,
                peerDeviceId = senderId,
                direction = TransferDirection.INCOMING,
                status = TransferStatus.UPLOADING,
                totalChunks = chunkCount,
                chunksUploaded = 0,
                transferId = transferId,
                mode = "streaming",
                negotiatedMode = "streaming",
            ))
        }

        val dir = java.io.File(android.os.Environment.getExternalStorageDirectory(), "DesktopConnector")
        if (!dir.exists() && !dir.mkdirs()) {
            Log.e(TAG, "Could not create DesktopConnector directory")
            db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Cannot create download folder")
            return
        }
        val partsDir = java.io.File(dir, ".parts")
        if (!partsDir.exists() && !partsDir.mkdirs()) {
            Log.e(TAG, "Could not create DesktopConnector/.parts directory")
            db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Cannot create parts folder")
            return
        }
        val finalFile = pickUniqueTarget(dir, fileName)
        val tempFile = java.io.File(partsDir, ".incoming_${transferId}.part")
        tempFile.delete()  // clear stale partial from a prior aborted run

        // D.6b: chunk-by-chunk state machine lives in downloadStreamLoop
        // as a pure function; this caller owns the IO + DB concerns
        // (file write, wake lock refresh, Room progress). Field
        // ownership during this call: PollService writes status,
        // chunksUploaded, totalChunks, abortReason, failureReason; the
        // delivery tracker owns deliveryChunks/Total on OUTGOING rows
        // (not relevant here — this path is INCOMING).
        var terminalState = "running"
        val outcome: StreamReceiveOutcome
        try {
            val fos = java.io.FileOutputStream(tempFile)
            outcome = java.io.BufferedOutputStream(fos).use { out ->
                downloadStreamLoop(
                    chunkCount = chunkCount,
                    downloadChunk = { index -> api.downloadChunkTyped(transferId, index) },
                    decrypt = { bytes ->
                        try {
                            CryptoUtils.decryptBlob(bytes, symmetricKey)
                        } catch (e: Exception) {
                            AppLog.log("Recv",
                                "transfer.chunk.decrypt_failed chunk_index=? error_kind=${e.javaClass.simpleName}",
                                "warning")
                            null
                        }
                    },
                    writeChunk = { _, plaintext ->
                        out.write(plaintext)
                        out.flush()
                        // fsync — see durability contract in KDoc above.
                        try { fos.fd.sync() } catch (_: Exception) {}
                    },
                    ackChunk = { index ->
                        val acked = try { api.ackChunk(transferId, index) } catch (_: Exception) { false }
                        if (!acked) {
                            AppLog.log("Recv",
                                "transfer.chunk.ack_failed transfer_id=${transferId.take(12)} chunk_index=$index",
                                "warning")
                        }
                        acked
                    },
                    onProgress = { index ->
                        wakeLock.acquire(2 * 60 * 1000L)  // refresh per chunk
                        db.transferDao().updateProgress(dbRowId, index + 1, chunkCount)
                    },
                    isCancelled = { db.transferDao().exists(dbRowId) == 0 },
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "Streaming write failed: ${e.message}", e)
            db.transferDao().markFailedWithReason(dbRowId, "write_failed")
            tempFile.delete()
            return
        }

        // Map the pure state machine's outcome onto Room + terminal
        // side-effects. Caller keeps DB / server-DELETE wiring here so
        // the state machine stays pure.
        when (outcome) {
            is StreamReceiveOutcome.Complete -> {
                terminalState = "complete"  // .part survives to finalize below
            }
            is StreamReceiveOutcome.AbortedByUpstream -> {
                val reason = outcome.reason ?: "sender_abort"
                AppLog.log("Recv",
                    "transfer.download.aborted_by_sender transfer_id=${transferId.take(12)} reason=$reason")
                db.transferDao().markAborted(dbRowId, reason)
                tempFile.delete()
                return
            }
            is StreamReceiveOutcome.RecipientAborted -> {
                AppLog.log("Recv",
                    "transfer.download.recipient_aborted transfer_id=${transferId.take(12)} reason=${outcome.reason}",
                    "warning")
                try { api.abortTransfer(transferId, "recipient_abort") } catch (_: Exception) {}
                db.transferDao().markAborted(dbRowId, "recipient_abort")
                if (outcome.reason != "recipient_abort") {
                    // Additionally mark FAILED with a typed failureReason
                    // for the non-user-cancel paths (stall_timeout, network).
                    db.transferDao().markFailedWithReason(dbRowId, outcome.reason)
                }
                tempFile.delete()
                return
            }
            is StreamReceiveOutcome.AuthLost -> {
                AppLog.log("Recv",
                    "transfer.download.auth_error transfer_id=${transferId.take(12)}",
                    "warning")
                tempFile.delete()
                return
            }
        }

        // Atomic finalize (same pattern as classic). No transfer-level ACK
        // — the per-chunk ACK on the last chunk already set downloaded=1
        // server-side.
        if (!tempFile.renameTo(finalFile)) {
            Log.e(TAG, "Rename temp → final failed for $transferId")
            tempFile.delete()
            db.transferDao().updateStatus(dbRowId, TransferStatus.FAILED, "Cannot finalize download")
            return
        }
        val finalSize = finalFile.length()
        AppLog.log("Recv", "transfer.download.completed transfer_id=${transferId.take(12)} bytes=$finalSize name=${finalFile.name} mode=streaming")

        // Notify MediaStore so the file appears in gallery/pickers.
        android.media.MediaScannerConnection.scanFile(
            this, arrayOf(finalFile.absolutePath), null, null
        )

        val stillExists = db.transferDao().exists(dbRowId) > 0
        db.transferDao().completeDownload(
            dbRowId, TransferStatus.COMPLETE, Uri.fromFile(finalFile).toString(), fileName, finalSize
        )
        db.transferDao().trimHistory()
        if (stillExists) {
            showTransferNotification(fileName)
        }
        terminalState = "complete"
    }

    /** Choose a non-colliding filename in [dir] by appending _1, _2, ... before the extension. */
    private fun pickUniqueTarget(dir: java.io.File, fileName: String): java.io.File {
        var target = java.io.File(dir, fileName)
        var counter = 1
        while (target.exists()) {
            val stem = fileName.substringBeforeLast(".")
            val ext = fileName.substringAfterLast(".", "")
            target = java.io.File(dir, "${stem}_${counter}${if (ext.isNotEmpty()) ".$ext" else ""}")
            counter++
        }
        return target
    }

    /**
     * Download + decrypt one chunk with 3-attempt retry. Retries on either
     * a missing body or an AES-GCM authentication failure. The latter
     * defends against server-side races where a concurrent upload causes
     * the reader to see partial bytes — the server's atomic rename is the
     * primary fix; this is belt-and-suspenders. Returns plaintext or null
     * on terminal failure.
     */
    private suspend fun downloadAndDecryptChunk(
        api: ApiClient, transferId: String, index: Int, symmetricKey: ByteArray,
    ): ByteArray? {
        for (attempt in 1..3) {
            val encrypted = api.downloadChunk(transferId, index)
            if (encrypted != null) {
                try {
                    return CryptoUtils.decryptBlob(encrypted, symmetricKey)
                } catch (e: Exception) {
                    AppLog.log("Recv", "transfer.chunk.failed chunk_index=$index attempt=$attempt/3 error_kind=${e.javaClass.simpleName}")
                }
            } else {
                AppLog.log("Recv", "transfer.chunk.failed chunk_index=$index attempt=$attempt/3 reason=no_body")
            }
            if (attempt < 3) delay(2000L * attempt)
        }
        return null
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
                    AppLog.log("Delivery", "delivery.tracker.skipped reason=previous_in_flight")
                } else {
                    val api = ApiClient(prefs.serverUrl!!, prefs.deviceId!!, prefs.authToken!!)
                    inFlightJob = scope.launch {
                        try {
                            withTimeout(750) { runDeliveryPoll(api, trackedIds, db) }
                        } catch (_: TimeoutCancellationException) {
                            AppLog.log("Delivery", "delivery.tracker.skipped reason=poll_timeout_750ms")
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
                // Paint whatever the server reports. For classic rows,
                // `downloaded` is 0 until state flips to in_progress
                // (recipient started draining post-upload), so this is
                // byte-for-byte the same behaviour as before. For
                // streaming rows, the server reports `chunks_downloaded >
                // 0` WHILE state is still "not_started" (lifecycle stays
                // in UPLOADING until complete=1 even when the recipient
                // is draining in parallel — see
                // TransferStatusMapper::toProtocolStatus). The earlier
                // `if (state == "in_progress") downloaded else 0` clause
                // discarded this mid-stream progress and kept
                // deliveryChunks at 0 throughout the streaming upload,
                // which in turn kept `maybeFlipToSending` from ever
                // flipping the sender row to SENDING.
                db.transferDao().updateDeliveryProgress(tid, downloaded, total)
            } else if (now - prev!!.second > DELIVERY_STALL_TIMEOUT_MS) {
                // D.4b: stall semantics differ by mode.
                //
                //  Classic: give up on this tid. The sender has finished
                //     uploading long ago and the recipient has been silent
                //     for 2 min — the long-poll inline sent_status and
                //     app-restart delivery check will still catch eventual
                //     delivery, so safe to quit HTTP-hammering.
                //
                //  Streaming: only clear the Y display. The sender may
                //     still be actively uploading on this same row; the
                //     recipient may simply be slower than the sender. We
                //     keep polling so that if `chunks_downloaded` advances
                //     again, the tracker resumes painting immediately.
                //     The sender's own budgets (30 min WAITING_STREAM,
                //     per-chunk 120 s network) catch the truly-dead cases.
                val mode = db.transferDao().getNegotiatedModeByTransferId(tid)
                val stallSeconds = (now - prev.second) / 1000
                if (mode == "streaming") {
                    AppLog.log("Delivery",
                        "delivery.tracker.stall transfer_id=${tid.take(12)} stall_seconds=$stallSeconds mode=streaming action=cleared_y_kept_polling")
                    db.transferDao().clearDeliveryProgress(tid)
                    trackerLastProgress.remove(tid)
                    // Deliberately NOT adding to trackerGaveUp — the next
                    // advance re-engages the tracker without a restart.
                } else {
                    AppLog.log("Delivery",
                        "delivery.tracker.stall transfer_id=${tid.take(12)} stall_seconds=$stallSeconds mode=classic action=gave_up")
                    db.transferDao().clearDeliveryProgress(tid)
                    trackerGaveUp.add(tid)
                    trackerLastProgress.remove(tid)
                }
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

    private fun handleFnTransfer(fileName: String, data: ByteArray, senderId: String?): String {
        val message = MessageAdapters.fromFnTransfer(fileName, data, senderId = senderId)
        if (message == null) return fileName

        when (message.type) {
            MessageType.CLIPBOARD_TEXT -> {
                if (!messageDispatcher.dispatch(message)) return fileName
                val text = message.payload["text"] as? String ?: return fileName
                val hasUrl = com.desktopconnector.ui.containsSingleUrl(text)
                return if (hasUrl) text else if (text.length > 40) text.take(40) + "..." else text
            }
            MessageType.CLIPBOARD_IMAGE -> {
                AppLog.log("Clipboard", "clipboard.image.skipped reason=handled_as_image_file")
                return fileName
            }
            MessageType.PAIRING_UNPAIR -> {
                messageDispatcher.dispatch(message)
                return "Unpaired by desktop"
            }
            else -> return fileName
        }
    }

    private fun pushTextToClipboard(text: String) {
        val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        val clip = ClipData.newPlainText("Desktop Connector", text)
        clipboard.setPrimaryClip(clip)
        AppLog.log("Clipboard", "clipboard.write_text.succeeded length=${text.length}")
    }

    private suspend fun saveClipboardImageTransfer(
        data: ByteArray,
        mimeType: String,
        transferId: String,
        senderId: String,
        prefs: AppPreferences,
        db: AppDatabase,
    ): String? {
        val imageType = clipboardImageType(mimeType, data)
        val file = saveFile("clipboard-image${imageType.first}", data) ?: return null
        val size = file.length()
        AppLog.log("Recv", "clipboard.image.saved bytes=$size name=${file.name}")
        db.transferDao().insert(QueuedTransfer(
            contentUri = Uri.fromFile(file).toString(),
            displayName = file.name,
            displayLabel = file.name,
            mimeType = imageType.second,
            sizeBytes = size,
            peerDeviceId = senderId,
            direction = TransferDirection.INCOMING,
            status = TransferStatus.COMPLETE,
            transferId = transferId,
        ))
        db.transferDao().trimHistory()
        showTransferNotification(file.name)
        return file.name
    }

    private fun clipboardImageType(mimeType: String, data: ByteArray): Pair<String, String> {
        val normalized = mimeType.substringBefore(";").trim().lowercase()
        when (normalized) {
            "image/png" -> return ".png" to "image/png"
            "image/jpeg", "image/jpg" -> return ".jpg" to "image/jpeg"
            "image/gif" -> return ".gif" to "image/gif"
            "image/webp" -> return ".webp" to "image/webp"
            "image/bmp" -> return ".bmp" to "image/bmp"
            "image/tiff" -> return ".tiff" to "image/tiff"
            "image/heic" -> return ".heic" to "image/heic"
            "image/heif" -> return ".heif" to "image/heif"
            "image/svg+xml" -> return ".svg" to "image/svg+xml"
        }

        if (hasPrefix(data, 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)) {
            return ".png" to "image/png"
        }
        if (hasPrefix(data, 0xFF, 0xD8, 0xFF)) {
            return ".jpg" to "image/jpeg"
        }
        if (hasAsciiPrefix(data, "GIF87a") || hasAsciiPrefix(data, "GIF89a")) {
            return ".gif" to "image/gif"
        }
        if (data.size >= 12 && hasAsciiPrefix(data, "RIFF") && asciiAt(data, 8, "WEBP")) {
            return ".webp" to "image/webp"
        }
        if (hasAsciiPrefix(data, "BM")) {
            return ".bmp" to "image/bmp"
        }
        if (hasPrefix(data, 0x49, 0x49, 0x2A, 0x00) || hasPrefix(data, 0x4D, 0x4D, 0x00, 0x2A)) {
            return ".tiff" to "image/tiff"
        }

        return ".png" to "image/png"
    }

    private fun hasPrefix(data: ByteArray, vararg prefix: Int): Boolean {
        if (data.size < prefix.size) return false
        return prefix.indices.all { (data[it].toInt() and 0xFF) == prefix[it] }
    }

    private fun hasAsciiPrefix(data: ByteArray, prefix: String): Boolean {
        return asciiAt(data, 0, prefix)
    }

    private fun asciiAt(data: ByteArray, offset: Int, value: String): Boolean {
        if (data.size < offset + value.length) return false
        return value.indices.all { ((data[offset + it].toInt() and 0xFF).toChar()) == value[it] }
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
            .setColor(BRAND_ACCENT)
            .setContentTitle("Received")
            .setContentText(label)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()

        mgr.notify(System.currentTimeMillis().toInt(), notification)
    }
}
