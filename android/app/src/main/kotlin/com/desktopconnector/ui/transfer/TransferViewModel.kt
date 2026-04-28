package com.desktopconnector.ui.transfer

import android.app.Application
import android.content.ClipboardManager
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import android.util.Log
import android.widget.Toast
import androidx.core.content.FileProvider
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.desktopconnector.crypto.CryptoUtils
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppDatabase
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.data.BatteryStatsDumper
import com.desktopconnector.data.QueuedTransfer
import com.desktopconnector.data.TransferDirection
import com.desktopconnector.data.TransferStatus
import com.desktopconnector.network.ApiClient
import com.desktopconnector.network.ConnectionManager
import com.desktopconnector.network.ConnectionState
import com.desktopconnector.network.UploadWorker
import com.desktopconnector.util.Installer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream
import java.util.UUID

class TransferViewModel(application: Application) : AndroidViewModel(application) {

    private val prefs = AppPreferences(application)
    private val keyManager = KeyManager(application)
    private val db = AppDatabase.getInstance(application)
    val connectionManager = ConnectionManager(prefs.serverUrl ?: "")

    private val _transfers = MutableStateFlow<List<QueuedTransfer>>(emptyList())
    val transfers: StateFlow<List<QueuedTransfer>> = _transfers.asStateFlow()

    private val _connectionState = MutableStateFlow(ConnectionState.DISCONNECTED)
    val connectionState: StateFlow<ConnectionState> = _connectionState.asStateFlow()

    private val _statusText = MutableStateFlow("Disconnected")
    val statusText: StateFlow<String> = _statusText.asStateFlow()

    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    // Link dialog state — set when user taps a link item
    private val _linkDialog = MutableStateFlow<Pair<String, String>?>(null) // (url, fullText)
    val linkDialog: StateFlow<Pair<String, String>?> = _linkDialog.asStateFlow()
    fun dismissLinkDialog() { _linkDialog.value = null }

    val pairedDeviceName: String
        get() = keyManager.getFirstPairedDevice()?.name ?: ""

    private val _isPaired = MutableStateFlow(keyManager.hasPairedDevice())
    val isPaired: StateFlow<Boolean> = _isPaired.asStateFlow()

    init {
        // Pass auth credentials to connection manager for heartbeat
        if (prefs.serverUrl != null) {
            connectionManager.serverUrl = prefs.serverUrl!!
            connectionManager.deviceId = prefs.deviceId ?: ""
            connectionManager.authToken = prefs.authToken ?: ""
        }

        // Feed every authenticated HTTP verdict (from the view-model's
        // ApiClient and PollService's ApiClients alike) into the counter
        // that decides when to surface the "re-pair" banner.
        viewModelScope.launch(Dispatchers.IO) {
            ApiClient.authObservations.collect { observation ->
                connectionManager.observeAuth(observation)
            }
        }

        // Health check loop — pings server, then waits for backoff or 15s
        viewModelScope.launch(Dispatchers.IO) {
            while (true) {
                if (prefs.serverUrl != null) {
                    connectionManager.serverUrl = prefs.serverUrl!!
                    connectionManager.deviceId = prefs.deviceId ?: ""
                    connectionManager.authToken = prefs.authToken ?: ""
                    val reachable = connectionManager.checkConnection()

                    if (!reachable) {
                        // Wait for backoff duration (UI tick loop updates countdown)
                        val backoff = connectionManager.retryInfo.value.currentBackoff
                        delay((backoff * 1000).toLong())
                    } else {
                        delay(15_000)
                    }
                } else {
                    delay(5_000)
                }
            }
        }

        // UI tick — updates status text and effective connection state every
        // second. Effective state folds auth-invalid into DISCONNECTED, so a
        // latched 401/403 paints the status dot offline even while network
        // reachability to /api/health is fine.
        viewModelScope.launch(Dispatchers.IO) {
            while (true) {
                _connectionState.value = connectionManager.effectiveState.value
                _statusText.value = connectionManager.getStatusText()
                delay(1_000)
            }
        }

        // Refresh transfer list + pairing state periodically
        viewModelScope.launch {
            while (true) {
                refreshTransfers()
                _isPaired.value = keyManager.hasPairedDevice()
                delay(2000)
            }
        }
    }

    /** Fetch the recent transfer list and, in the same pass, flip any
     *  WAITING row older than 30 minutes to FAILED. Such a row can't
     *  have a live UploadWorker attached (cap enforced there too), so
     *  it's a zombie from an app restart / WM chain cancellation —
     *  leaving it as WAITING would spin a yellow chip forever. Scrub
     *  + snapshot share one getRecent() so the UI never renders the
     *  pre-scrub state for a tick. */
    private suspend fun refreshTransfers() {
        val rows = withContext(Dispatchers.IO) {
            val nowMs = System.currentTimeMillis()
            val createdCutoffSec = nowMs / 1000 - 30 * 60
            val waitingStreamCutoffMs = nowMs - 30 * 60 * 1000L
            val fetched = db.transferDao().getRecent()

            // Classic WAITING scrub (pre-streaming): row created more than
            // 30 min ago and still WAITING — no live UploadWorker. Mark
            // FAILED("quota exceeded") so the UI stops spinning yellow.
            val classicZombies = fetched
                .filter { it.status == TransferStatus.WAITING && it.createdAt < createdCutoffSec }
                .map { it.id }

            // D.5: streaming WAITING_STREAM scrub. Same 30-min window, but
            // clocked from `waitingStartedAt` (ms) stamped by D.4a's
            // markWaitingStream on the first 507. `quota_timeout` here
            // matches the sender loop's terminal reason so scrubbed rows
            // render with the typed failureReason.
            val streamingZombies = fetched
                .filter {
                    it.status == TransferStatus.WAITING_STREAM
                        && it.waitingStartedAt != null
                        && it.waitingStartedAt < waitingStreamCutoffMs
                }
                .map { it.id }

            classicZombies.forEach {
                db.transferDao().updateStatus(it, TransferStatus.FAILED, "quota exceeded")
            }
            streamingZombies.forEach {
                db.transferDao().markFailedWithReason(it, "quota_timeout")
            }

            val scrubbedIds = (classicZombies + streamingZombies).toSet()
            if (scrubbedIds.isEmpty()) fetched
            else fetched.map { row ->
                if (row.id !in scrubbedIds) row
                else if (row.status == TransferStatus.WAITING_STREAM) row.copy(
                    status = TransferStatus.FAILED,
                    failureReason = "quota_timeout",
                    errorMessage = "quota_timeout",
                )
                else row.copy(
                    status = TransferStatus.FAILED,
                    errorMessage = "quota exceeded",
                )
            }
        }
        _transfers.value = rows
    }

    fun onRefresh() {
        viewModelScope.launch {
            _isRefreshing.value = true
            refreshTransfers()
            withContext(Dispatchers.IO) {
                // Reset backoff — user explicitly asked to retry now
                connectionManager.tryNow()
                _connectionState.value = connectionManager.state.value
            }
            _isRefreshing.value = false
        }
    }

    fun queueFiles(uris: List<Uri>) {
        val app = getApplication<Application>()
        val paired = keyManager.getFirstPairedDevice() ?: return

        viewModelScope.launch(Dispatchers.IO) {
            for (uri in uris) {
                val (name, size, mime) = getFileInfo(app, uri)
                try {
                    app.contentResolver.takePersistableUriPermission(
                        uri, android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION
                    )
                } catch (_: SecurityException) {}

                val transfer = QueuedTransfer(
                    contentUri = uri.toString(),
                    displayName = name,
                    displayLabel = name,
                    mimeType = mime,
                    sizeBytes = size,
                    recipientDeviceId = paired.deviceId,
                )
                val id = db.transferDao().insert(transfer)
                UploadWorker.enqueue(app, id)
            }
            refreshTransfers()
        }
    }

    fun sendClipboard() {
        val app = getApplication<Application>()
        val paired = keyManager.getFirstPairedDevice()
        if (paired == null) {
            viewModelScope.launch(Dispatchers.Main) {
                Toast.makeText(app, "No paired device", Toast.LENGTH_SHORT).show()
            }
            return
        }

        viewModelScope.launch(Dispatchers.IO) {
            val clipboard = app.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager

            val clip = withContext(Dispatchers.Main) { clipboard.primaryClip }
            if (clip == null || clip.itemCount == 0) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(app, "Clipboard is empty", Toast.LENGTH_SHORT).show()
                }
                return@launch
            }

            val item = clip.getItemAt(0)

            // Try to get URI first (image/file)
            val uri = item.uri
            if (uri != null) {
                val mime = app.contentResolver.getType(uri)
                if (mime != null && (mime.startsWith("image/") || mime.startsWith("video/") || mime.startsWith("application/"))) {
                    // Clipboard has a file/image URI — send it as a file with clipboard flag
                    queueClipboardFile(uri, paired.deviceId)
                    withContext(Dispatchers.Main) {
                        Toast.makeText(app, "Sending clipboard image...", Toast.LENGTH_SHORT).show()
                    }
                    return@launch
                }
            }

            // Fall back to text
            val text = item.coerceToText(app)?.toString()
            if (!text.isNullOrEmpty()) {
                queueClipboardText(text, paired.deviceId)
                val preview = if (text.length > 30) text.take(30) + "..." else text
                withContext(Dispatchers.Main) {
                    Toast.makeText(app, "Sending: $preview", Toast.LENGTH_SHORT).show()
                }
                return@launch
            }

            withContext(Dispatchers.Main) {
                Toast.makeText(app, "Unsupported clipboard content", Toast.LENGTH_SHORT).show()
            }
        }
    }

    fun sendClipboardText(text: String) {
        val app = getApplication<Application>()
        val paired = keyManager.getFirstPairedDevice() ?: return
        viewModelScope.launch(Dispatchers.IO) {
            queueClipboardText(text, paired.deviceId)
        }
    }

    private suspend fun queueClipboardText(text: String, recipientId: String) {
        val app = getApplication<Application>()
        val data = text.toByteArray()
        // Keep full text if it contains a URL (so we can open it on click), truncate otherwise
        val hasUrl = com.desktopconnector.ui.containsSingleUrl(text)
        val preview = if (hasUrl) text else if (text.length > 40) text.take(40) + "..." else text

        val tempFile = File(app.cacheDir, ".fn.clipboard.text_${System.currentTimeMillis()}")
        FileOutputStream(tempFile).use { it.write(data) }

        val uri = Uri.fromFile(tempFile)
        val transfer = QueuedTransfer(
            contentUri = uri.toString(),
            displayName = ".fn.clipboard.text",
            displayLabel = preview,
            mimeType = "text/plain",
            sizeBytes = data.size.toLong(),
            recipientDeviceId = recipientId,
        )
        val id = db.transferDao().insert(transfer)
        UploadWorker.enqueue(app, id)
        refreshTransfers()
    }

    private suspend fun queueClipboardFile(uri: Uri, recipientId: String) {
        val app = getApplication<Application>()
        val (_, size, mime) = getFileInfo(app, uri)

        val transfer = QueuedTransfer(
            contentUri = uri.toString(),
            displayName = ".fn.clipboard.image",
            displayLabel = "Clipboard image",
            mimeType = mime,
            sizeBytes = size,
            recipientDeviceId = recipientId,
        )
        val id = db.transferDao().insert(transfer)
        UploadWorker.enqueue(app, id)
        refreshTransfers()
    }

    fun resend(transfer: QueuedTransfer) {
        val app = getApplication<Application>()
        val paired = keyManager.getFirstPairedDevice() ?: return

        viewModelScope.launch(Dispatchers.IO) {
            val newTransfer = transfer.copy(
                id = 0,
                status = TransferStatus.QUEUED,
                chunksUploaded = 0,
                errorMessage = null,
                createdAt = System.currentTimeMillis() / 1000,
                recipientDeviceId = paired.deviceId,
            )
            val id = db.transferDao().insert(newTransfer)
            UploadWorker.enqueue(app, id)
            refreshTransfers()
            withContext(Dispatchers.Main) {
                Toast.makeText(app, "Resending...", Toast.LENGTH_SHORT).show()
            }
        }
    }

    fun openLink(url: String) {
        val app = getApplication<Application>()
        try {
            val intent = android.content.Intent(android.content.Intent.ACTION_VIEW, Uri.parse(url)).apply {
                addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            app.startActivity(intent)
        } catch (e: Exception) {
            Toast.makeText(app, "Cannot open link", Toast.LENGTH_SHORT).show()
        }
        _linkDialog.value = null
    }

    fun copyLinkToClipboard(text: String) {
        val app = getApplication<Application>()
        val clipboard = app.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(android.content.ClipData.newPlainText("Desktop Connector", text))
        Toast.makeText(app, "Copied to clipboard", Toast.LENGTH_SHORT).show()
        _linkDialog.value = null
    }

    fun onItemClick(transfer: QueuedTransfer) {
        val app = getApplication<Application>()
        val isClipboard = transfer.displayName.startsWith(".fn.clipboard")
        val label = transfer.displayLabel.ifEmpty { transfer.displayName }

        com.desktopconnector.data.AppLog.log("Click", "name=${transfer.displayName} label=${label} mime=${transfer.mimeType} uri=${transfer.contentUri}")

        // Check for link — show dialog instead of immediate action
        if (isClipboard) {
            val url = com.desktopconnector.ui.extractSingleUrl(label)
            if (url != null) {
                _linkDialog.value = Pair(url, label)
                return
            }
        }

        viewModelScope.launch {
            if (isClipboard) {
                // Push to clipboard
                val content = if (transfer.contentUri.isNotEmpty()) {
                    try {
                        val uri = Uri.parse(transfer.contentUri)
                        if (uri.scheme == "file") {
                            java.io.File(uri.path!!).readText()
                        } else {
                            app.contentResolver.openInputStream(uri)?.bufferedReader()?.readText()
                        }
                    } catch (_: Exception) { null }
                } else null

                if (content != null) {
                    withContext(Dispatchers.Main) {
                        val clipboard = app.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                        clipboard.setPrimaryClip(android.content.ClipData.newPlainText("Desktop Connector", content))
                        Toast.makeText(app, "Copied to clipboard", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    // For received clipboard items, use the display label as the content
                    val label = transfer.displayLabel
                    if (label.isNotEmpty() && label != "Clipboard image") {
                        withContext(Dispatchers.Main) {
                            val clipboard = app.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                            clipboard.setPrimaryClip(android.content.ClipData.newPlainText("Desktop Connector", label))
                            Toast.makeText(app, "Copied to clipboard", Toast.LENGTH_SHORT).show()
                        }
                    } else {
                        withContext(Dispatchers.Main) {
                            Toast.makeText(app, "Clipboard content no longer available", Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            } else if (transfer.displayName.endsWith(".apk") || transfer.displayLabel.endsWith(".apk") || transfer.mimeType.contains("android.package")) {
                withContext(Dispatchers.Main) {
                    val outcome = try {
                        val file = java.io.File(Uri.parse(transfer.contentUri).path!!)
                        Installer.installApk(app, file)
                    } catch (e: Exception) {
                        com.desktopconnector.data.AppLog.log("APK", "Error: ${e.message}")
                        Installer.InstallStartOutcome.ERROR
                    }
                    when (outcome) {
                        Installer.InstallStartOutcome.FILE_GONE ->
                            Toast.makeText(app, "APK file no longer exists", Toast.LENGTH_SHORT).show()
                        Installer.InstallStartOutcome.MISSING_PERMISSION ->
                            Toast.makeText(app, "Please allow installing from unknown sources", Toast.LENGTH_LONG).show()
                        Installer.InstallStartOutcome.ERROR ->
                            Toast.makeText(app, "Cannot open APK", Toast.LENGTH_LONG).show()
                        Installer.InstallStartOutcome.LAUNCHED -> {}
                    }
                }
            } else {
                // Open file — try contentUri for sent, or DesktopConnector dir for received
                var fileUri: Uri? = null

                if (transfer.direction == TransferDirection.INCOMING) {
                    // Received files: check DesktopConnector dir first, then contentUri
                    val dir = java.io.File(android.os.Environment.getExternalStorageDirectory(), "DesktopConnector")
                    val file = java.io.File(dir, transfer.displayLabel.ifEmpty { transfer.displayName })
                    fileUri = if (file.exists()) Uri.fromFile(file)
                              else if (transfer.contentUri.isNotEmpty()) Uri.parse(transfer.contentUri)
                              else null
                } else if (transfer.contentUri.isNotEmpty()) {
                    // Sent files: try the original URI (may have expired permission)
                    val uri = Uri.parse(transfer.contentUri)
                    if (uri.scheme == "file") {
                        val file = java.io.File(uri.path!!)
                        if (file.exists()) fileUri = uri
                    } else {
                        // content:// URI — check if we still have read access
                        try {
                            app.contentResolver.openInputStream(uri)?.close()
                            fileUri = uri
                        } catch (_: Exception) {}
                    }
                }

                if (fileUri != null) {
                    try {
                        val actualUri = if (fileUri.scheme == "file") {
                            androidx.core.content.FileProvider.getUriForFile(
                                app, "${app.packageName}.fileprovider", java.io.File(fileUri.path!!)
                            )
                        } else fileUri

                        val intent = android.content.Intent(android.content.Intent.ACTION_VIEW).apply {
                            setDataAndType(actualUri, transfer.mimeType)
                            addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                        }
                        app.startActivity(intent)
                    } catch (e: Exception) {
                        withContext(Dispatchers.Main) {
                            Toast.makeText(app, "Cannot open: ${e.message}", Toast.LENGTH_SHORT).show()
                        }
                    }
                } else {
                    withContext(Dispatchers.Main) {
                        val msg = if (transfer.direction == TransferDirection.OUTGOING)
                            "Original file no longer accessible" else "File no longer exists"
                        Toast.makeText(app, msg, Toast.LENGTH_SHORT).show()
                    }
                }
            }
        }
    }

    fun sendUnpairNotification(pairedDeviceId: String) {
        val app = getApplication<Application>()
        val paired = keyManager.getPairedDevice(pairedDeviceId) ?: return

        viewModelScope.launch(Dispatchers.IO) {
            try {
                val prefs = AppPreferences(app)
                val symmetricKey = android.util.Base64.decode(paired.symmetricKeyB64, android.util.Base64.NO_WRAP)
                val api = com.desktopconnector.network.ApiClient(
                    prefs.serverUrl ?: "", prefs.deviceId ?: "", prefs.authToken ?: ""
                )

                // Encrypt a tiny .fn.unpair payload (one chunk)
                val data = "unpair".toByteArray()
                val baseNonce = keyManager.generateBaseNonce()
                val encryptedMeta = keyManager.buildEncryptedMetadata(
                    fileName = ".fn.unpair",
                    mimeType = "application/octet-stream",
                    fileSize = data.size.toLong(),
                    chunkCount = 1,
                    baseNonce = baseNonce,
                    symmetricKey = symmetricKey,
                )
                val encryptedChunk = keyManager.encryptChunk(data, baseNonce, 0, symmetricKey)

                val transferId = java.util.UUID.randomUUID().toString()
                if (api.initTransfer(transferId, pairedDeviceId, encryptedMeta, 1) == ApiClient.InitOutcome.OK) {
                    api.uploadChunk(transferId, 0, encryptedChunk)
                }
            } catch (e: Exception) {
                Log.w("TransferVM", "Failed to send unpair notification: ${e.message}")
            }
        }
    }

    fun sendLogsToDesktop(text: String, appendBatteryStats: Boolean = false) {
        val app = getApplication<Application>()
        val paired = keyManager.getFirstPairedDevice() ?: return

        viewModelScope.launch(Dispatchers.IO) {
            val combined = if (appendBatteryStats) {
                BatteryStatsDumper.capture(app.packageName) + text
            } else {
                text
            }
            val bytes = combined.toByteArray()

            val tempFile = java.io.File(app.cacheDir, "android_logs_${System.currentTimeMillis()}.txt")
            java.io.FileOutputStream(tempFile).use { it.write(bytes) }

            val transfer = QueuedTransfer(
                contentUri = Uri.fromFile(tempFile).toString(),
                displayName = "android_logs.txt",
                displayLabel = "App logs",
                mimeType = "text/plain",
                sizeBytes = bytes.size.toLong(),
                recipientDeviceId = paired.deviceId,
            )
            val id = db.transferDao().insert(transfer)
            UploadWorker.enqueue(app, id)
            refreshTransfers()
            withContext(Dispatchers.Main) {
                Toast.makeText(app, "Sending logs to desktop...", Toast.LENGTH_SHORT).show()
            }
        }
    }

    fun downloadLogsToPhone(text: String, appendBatteryStats: Boolean = false) {
        val app = getApplication<Application>()

        viewModelScope.launch(Dispatchers.IO) {
            try {
                val combined = if (appendBatteryStats) {
                    BatteryStatsDumper.capture(app.packageName) + text
                } else {
                    text
                }

                val timestamp = System.currentTimeMillis()
                val filename = "android_logs_$timestamp.txt"

                // Save to DesktopConnector folder
                val dcFolder = File(
                    android.os.Environment.getExternalStoragePublicDirectory(android.os.Environment.DIRECTORY_DOCUMENTS),
                    "DesktopConnector"
                )
                dcFolder.mkdirs()

                val logFile = File(dcFolder, filename)
                logFile.writeText(combined)

                // Notify MediaStore to index the file
                android.media.MediaScannerConnection.scanFile(
                    app,
                    arrayOf(logFile.absolutePath),
                    null,
                    null
                )

                withContext(Dispatchers.Main) {
                    Toast.makeText(app, "Logs saved to Documents/DesktopConnector/$filename", Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(app, "Failed to save logs: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    fun deleteTransfer(transfer: QueuedTransfer) {
        viewModelScope.launch(Dispatchers.IO) {
            db.transferDao().delete(transfer.id)
            refreshTransfers()
        }
    }

    /** Cancel a still-in-flight transfer + remove from history.
     *
     *  Fires server DELETE with a typed abort reason so the other side
     *  learns quickly (sender aborts → recipient gets 410 on next chunk
     *  GET; recipient aborts → sender gets 410 on next chunk upload).
     *  Local row goes away regardless of whether the server call
     *  succeeds — best-effort; server GCs orphans via its own expiry
     *  timer.
     *
     *  D.5: reason is direction-aware so streaming transfers (where
     *  either party can abort) use the protocol-correct reason. Classic
     *  outgoing rows land on `sender_abort` — byte-for-byte the same
     *  wire as the pre-streaming flow. Classic incoming rows rarely
     *  reach this path (classic receivers finish synchronously in the
     *  poller) but the branch is correct if they do.
     */
    fun cancelAndDelete(transfer: QueuedTransfer) {
        viewModelScope.launch(Dispatchers.IO) {
            val tid = transfer.transferId
            if (tid != null && prefs.serverUrl != null
                && prefs.deviceId != null && prefs.authToken != null) {
                val reason = when (transfer.direction) {
                    TransferDirection.OUTGOING -> "sender_abort"
                    TransferDirection.INCOMING -> "recipient_abort"
                }
                try {
                    ApiClient(prefs.serverUrl!!, prefs.deviceId!!, prefs.authToken!!)
                        .abortTransfer(tid, reason)
                } catch (_: Exception) {
                    // best effort
                }
            }
            // WorkManager may still re-trigger an outgoing worker for
            // waiting/uploading rows; cancel the chain first so the row
            // doesn't get recreated on retry. Incoming rows have no
            // upload-* work tag, so skip.
            if (transfer.direction == TransferDirection.OUTGOING) {
                androidx.work.WorkManager.getInstance(getApplication())
                    .cancelAllWorkByTag("upload-${transfer.id}")
            }
            db.transferDao().delete(transfer.id)
            com.desktopconnector.network.StoragePressure.clear()
            refreshTransfers()
        }
    }

    /** Is this row still "in flight" on the server? Drives the
     *  confirmation dialog before delete.
     *
     *  Terminal statuses (never in-flight): FAILED, ABORTED.
     *  Delivered rows (`delivered=1`) are terminal regardless of status
     *  — streaming rows stay in SENDING after the tracker flips the
     *  flag; classic rows stay in COMPLETE.
     *
     *  D.5: incoming streaming rows can legitimately sit in UPLOADING /
     *  WAITING_STREAM for a long time while downloading chunks. Mark
     *  them in-flight so the user gets a confirmation dialog before
     *  aborting the download. Classic incoming receives finish
     *  synchronously in the poller so they rarely reach the UI in a
     *  non-terminal state; no behaviour change for them.
     */
    fun isInFlight(transfer: QueuedTransfer): Boolean {
        if (transfer.status == TransferStatus.FAILED) return false
        if (transfer.status == TransferStatus.ABORTED) return false
        if (transfer.delivered) return false
        if (transfer.direction == TransferDirection.INCOMING) {
            return transfer.status == TransferStatus.UPLOADING
                || transfer.status == TransferStatus.WAITING_STREAM
        }
        return true
    }

    fun clearHistory() {
        viewModelScope.launch(Dispatchers.IO) {
            db.transferDao().clearAll()
            refreshTransfers()
        }
    }

    fun tryNow() {
        viewModelScope.launch(Dispatchers.IO) {
            connectionManager.tryNow()
            _connectionState.value = connectionManager.state.value
            _statusText.value = connectionManager.getStatusText()
        }
    }

    /** User tapped the "Re-pair" banner. Wipes the appropriate scope and
     *  clears the latched auth-failure flag. Caller navigates to the
     *  pairing screen. */
    fun repairFromAuthFailure() {
        val kind = connectionManager.authFailureKind.value ?: return
        viewModelScope.launch(Dispatchers.IO) {
            keyManager.removeAllPairedDevices()
            if (kind == com.desktopconnector.network.AuthFailureKind.CREDENTIALS_INVALID) {
                prefs.clearAuthCredentials()
                keyManager.resetKeypair()
            }
            // FCM token survives pair cycles on the device side but the
            // server's record of it just got wiped. Reset so the next
            // registerToken() POSTs rather than skipping on a matching
            // cached string.
            com.desktopconnector.network.FcmManager.reset(prefs)
            _isPaired.value = false
            connectionManager.clearAuthFailure()
        }
    }

    private fun getFileInfo(app: Application, uri: Uri): Triple<String, Long, String> {
        var name = "unknown"
        var size = 0L
        val mime = app.contentResolver.getType(uri) ?: "application/octet-stream"

        app.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
            if (cursor.moveToFirst()) {
                if (nameIndex >= 0) name = cursor.getString(nameIndex) ?: "unknown"
                if (sizeIndex >= 0) size = cursor.getLong(sizeIndex)
            }
        }
        return Triple(name, size, mime)
    }
}
