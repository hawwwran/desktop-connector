package com.desktopconnector.ui

import android.content.ContentUris
import android.graphics.Bitmap
import android.net.Uri
import android.provider.MediaStore
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.PullToRefreshDefaults
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.*
import androidx.compose.runtime.snapshots.SnapshotStateList
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.desktopconnector.R
import com.desktopconnector.data.QueuedTransfer
import com.desktopconnector.data.TransferDirection
import com.desktopconnector.data.TransferStatus
import com.desktopconnector.network.AuthFailureKind
import com.desktopconnector.network.ConnectionState
import com.desktopconnector.ui.theme.brandColors

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    connectionState: ConnectionState,
    transfers: List<QueuedTransfer>,
    isRefreshing: Boolean,
    authFailureKind: AuthFailureKind?,
    storageFull: Boolean,
    onFilesSelected: (List<Uri>) -> Unit,
    onSendClipboard: () -> Unit,
    onSendUri: (Uri) -> Unit,
    onItemClick: (QueuedTransfer) -> Unit,
    onDelete: (QueuedTransfer) -> Unit,
    onCancelInFlight: (QueuedTransfer) -> Unit,
    isInFlight: (QueuedTransfer) -> Boolean,
    onRefresh: () -> Unit,
    onNavigateSettings: () -> Unit,
    onNavigateDownloads: () -> Unit,
    onClearHistory: () -> Unit,
    onRepair: () -> Unit,
) {
    var confirmCandidate by remember { mutableStateOf<QueuedTransfer?>(null) }

    confirmCandidate?.let { target ->
        // D.5: dialog branches on direction. Incoming streaming rows
        // reach here when the user swipe-deletes mid-download
        // (isInFlight returns true for streaming incoming rows).
        // Outgoing rows hit the existing "Cancel delivery" messaging.
        val isIncoming = target.direction == TransferDirection.INCOMING
        val name = target.displayLabel.ifEmpty { target.displayName }
        AlertDialog(
            onDismissRequest = { confirmCandidate = null },
            title = { Text(if (isIncoming) "Cancel download?" else "Cancel delivery?") },
            text = {
                Text(
                    if (isIncoming)
                        "\u201c$name\u201d won't be saved."
                    else
                        "The recipient will no longer receive \u201c$name\u201d."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    val t = confirmCandidate
                    confirmCandidate = null
                    if (t != null) onCancelInFlight(t)
                }) {
                    Text(
                        if (isIncoming) "Cancel download" else "Cancel delivery",
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            },
            dismissButton = {
                TextButton(onClick = { confirmCandidate = null }) { Text("Keep") }
            },
        )
    }
    val filePicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        if (uris.isNotEmpty()) onFilesSelected(uris)
    }

    Scaffold(
        topBar = {
            val brand = MaterialTheme.brandColors
            val statusColor = when (connectionState) {
                ConnectionState.CONNECTED -> brand.connectionConnected
                ConnectionState.RECONNECTING -> brand.connectionReconnecting
                ConnectionState.DISCONNECTED -> brand.connectionDisconnected
            }
            val statusIcon = when (connectionState) {
                ConnectionState.DISCONNECTED -> R.drawable.ic_notif_disconnected
                else -> R.drawable.ic_notif_connected
            }
            TopAppBar(
                title = {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.clickable { onRefresh() },
                    ) {
                        Icon(
                            painter = androidx.compose.ui.res.painterResource(statusIcon),
                            contentDescription = when (connectionState) {
                                ConnectionState.CONNECTED -> "Connected"
                                ConnectionState.RECONNECTING -> "Reconnecting"
                                ConnectionState.DISCONNECTED -> "Disconnected"
                            },
                            tint = statusColor,
                            modifier = Modifier.size(20.dp),
                        )
                        Spacer(Modifier.width(10.dp))
                        Text("Desktop Connector")
                    }
                },
                actions = {
                    IconButton(onClick = onNavigateDownloads) {
                        Text("\uD83D\uDCC2", style = MaterialTheme.typography.titleMedium)
                    }
                    IconButton(onClick = onNavigateSettings) {
                        Icon(Icons.Default.Settings, "Settings")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                ),
            )
        },
    ) { padding ->
        val recentLoader = rememberRecentFilesLoader()
        val lifecycleOwner = androidx.lifecycle.compose.LocalLifecycleOwner.current
        DisposableEffect(lifecycleOwner) {
            val observer = androidx.lifecycle.LifecycleEventObserver { _, event ->
                if (event == androidx.lifecycle.Lifecycle.Event.ON_RESUME) {
                    recentLoader.refresh()
                }
            }
            lifecycleOwner.lifecycle.addObserver(observer)
            onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
        }

        val pullState = rememberPullToRefreshState()
        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = {
                recentLoader.refresh()
                onRefresh()
            },
            modifier = Modifier
                .padding(padding)
                .fillMaxSize(),
            state = pullState,
            indicator = {
                PullToRefreshDefaults.Indicator(
                    state = pullState,
                    isRefreshing = isRefreshing,
                    modifier = Modifier.align(Alignment.TopCenter),
                    color = com.desktopconnector.ui.theme.DcYellow500,
                    containerColor = com.desktopconnector.ui.theme.DcOrange700,
                )
            },
        ) {
            Column(modifier = Modifier.fillMaxSize()) {
                if (authFailureKind != null) {
                    AuthFailureBanner(
                        kind = authFailureKind,
                        onRepair = onRepair,
                    )
                }
                // Storage-full info banner. Unlike the auth banner this
                // one has no action button — the queue resolves on its
                // own as the recipient drains. Yellow surface + icon.
                if (storageFull && authFailureKind == null) {
                    StorageFullBanner()
                }
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 12.dp, vertical = 8.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Button(
                        onClick = onSendClipboard,
                        modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = MaterialTheme.colorScheme.secondaryContainer,
                            contentColor = MaterialTheme.colorScheme.onSecondaryContainer,
                        ),
                    ) {
                        Icon(
                            painter = androidx.compose.ui.res.painterResource(R.drawable.ic_clipboard),
                            contentDescription = null,
                            modifier = Modifier.size(18.dp),
                        )
                        Spacer(Modifier.width(6.dp))
                        Text("Send Clipboard")
                    }
                    Button(
                        onClick = { filePicker.launch(arrayOf("*/*")) },
                        modifier = Modifier.weight(1f),
                    ) {
                        Icon(Icons.Default.Share, null, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("Send Files")
                    }
                }

                RecentFilesStrip(loader = recentLoader, onSend = { uri -> onSendUri(uri) })

                var showClearDialog by remember { mutableStateOf(false) }

                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(start = 16.dp, end = 4.dp, top = 12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        "History",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    if (transfers.isNotEmpty()) {
                        IconButton(
                            onClick = { showClearDialog = true },
                            modifier = Modifier.size(32.dp),
                        ) {
                            Icon(
                                Icons.Default.Delete,
                                contentDescription = "Clear history",
                                modifier = Modifier.size(16.dp),
                                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }

                if (showClearDialog) {
                    AlertDialog(
                        onDismissRequest = { showClearDialog = false },
                        title = { Text("Clear history?") },
                        text = { Text("This will remove all transfer history entries.") },
                        confirmButton = {
                            TextButton(onClick = {
                                showClearDialog = false
                                onClearHistory()
                            }) { Text("Clear All", color = MaterialTheme.colorScheme.error) }
                        },
                        dismissButton = {
                            TextButton(onClick = { showClearDialog = false }) { Text("Cancel") }
                        },
                    )
                }

                if (transfers.isEmpty()) {
                    // verticalScroll gives PullToRefreshBox a nested-scroll
                    // surface so pull-to-refresh still fires over the empty
                    // state — the non-empty branch uses a LazyColumn which
                    // already participates in nested scroll.
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .weight(1f)
                            .verticalScroll(rememberScrollState()),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            "No transfers yet.\nSend files or clipboard to get started.",
                            style = MaterialTheme.typography.bodyLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            textAlign = TextAlign.Center,
                        )
                    }
                } else {
                    val historyListState = rememberLazyListState()
                    LaunchedEffect(transfers.firstOrNull()?.id) {
                        historyListState.animateScrollToItem(0)
                    }
                    LazyColumn(
                        state = historyListState,
                        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 16.dp, top = 4.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                        modifier = Modifier.weight(1f),
                    ) {
                        items(transfers, key = { it.id }) { transfer ->
                            SwipeToDeleteItem(
                                onDelete = {
                                    if (isInFlight(transfer)) {
                                        confirmCandidate = transfer
                                    } else {
                                        onDelete(transfer)
                                    }
                                },
                            ) {
                                TransferItem(transfer, onClick = { onItemClick(transfer) })
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun AuthFailureBanner(
    kind: AuthFailureKind,
    onRepair: () -> Unit,
) {
    val brand = MaterialTheme.brandColors
    val message = when (kind) {
        AuthFailureKind.CREDENTIALS_INVALID ->
            "Server doesn't recognise this device. Re-pair to restore the connection."
        AuthFailureKind.PAIRING_MISSING ->
            "Pairing was lost on the server. Re-pair to restore the connection."
    }
    androidx.compose.material3.Surface(
        color = MaterialTheme.colorScheme.errorContainer,
        contentColor = MaterialTheme.colorScheme.onErrorContainer,
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 8.dp),
        shape = MaterialTheme.shapes.medium,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(
                text = message,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.weight(1f),
            )
            Button(
                onClick = onRepair,
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.error,
                    contentColor = MaterialTheme.colorScheme.onError,
                ),
            ) { Text("Re-pair") }
        }
    }
}

@Composable
private fun StorageFullBanner() {
    val brand = MaterialTheme.brandColors
    // Yellow surface — matches the WAITING pill on the transfer row.
    androidx.compose.material3.Surface(
        color = com.desktopconnector.ui.theme.DcYellow500,
        contentColor = com.desktopconnector.ui.theme.DcBlue950,
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 8.dp),
        shape = MaterialTheme.shapes.medium,
    ) {
        Text(
            text = "Server storage full — your next send is queued and will resume when the recipient has downloaded earlier transfers.",
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
        )
    }
}

@Composable
private fun TransferItem(transfer: QueuedTransfer, onClick: () -> Unit) {
    val brand = MaterialTheme.brandColors
    val dim = MaterialTheme.colorScheme.onSurfaceVariant
    val statusColor = when {
        transfer.status == TransferStatus.QUEUED -> dim
        transfer.status == TransferStatus.PREPARING -> dim
        transfer.status == TransferStatus.WAITING -> brand.transferOutgoing  // yellow
        transfer.status == TransferStatus.WAITING_STREAM -> brand.transferOutgoing  // yellow (same pill as WAITING)
        transfer.status == TransferStatus.UPLOADING && transfer.direction == TransferDirection.INCOMING -> brand.transferIncoming
        transfer.status == TransferStatus.UPLOADING -> brand.transferOutgoing
        transfer.status == TransferStatus.SENDING && transfer.delivered -> dim   // post-delivery handoff window
        transfer.status == TransferStatus.SENDING -> brand.transferDelivering    // blue — overlapped upload/delivery
        transfer.status == TransferStatus.DELIVERING -> brand.transferDelivering // blue — reserved, parity with desktop
        transfer.status == TransferStatus.COMPLETE && transfer.direction == TransferDirection.INCOMING -> brand.connectionConnected
        transfer.status == TransferStatus.COMPLETE && transfer.delivered -> dim
        transfer.status == TransferStatus.COMPLETE && transfer.deliveryTotal > 0 -> brand.transferDelivering
        transfer.status == TransferStatus.COMPLETE -> dim
        transfer.status == TransferStatus.FAILED -> MaterialTheme.colorScheme.error
        transfer.status == TransferStatus.ABORTED -> MaterialTheme.colorScheme.error  // orange per brand palette
        else -> dim
    }
    val dirLabel = if (transfer.direction == TransferDirection.INCOMING) "\u2193 " else "\u2191 "
    val statusText = when (transfer.status) {
        TransferStatus.QUEUED -> "Queued"
        TransferStatus.PREPARING -> "Preparing"
        TransferStatus.WAITING -> "Waiting"
        TransferStatus.UPLOADING -> when (transfer.direction) {
            TransferDirection.INCOMING -> if (transfer.totalChunks > 0) "Downloading ${transfer.chunksUploaded}/${transfer.totalChunks}" else "Downloading"
            TransferDirection.OUTGOING -> if (transfer.totalChunks > 0) "Uploading ${transfer.chunksUploaded}/${transfer.totalChunks}" else "Uploading"
        }
        TransferStatus.COMPLETE -> when {
            transfer.direction == TransferDirection.INCOMING -> "Received"
            transfer.delivered -> "Delivered"
            transfer.deliveryTotal > 0 -> "Delivering ${transfer.deliveryChunks}/${transfer.deliveryTotal}"
            else -> "Sent"
        }
        // errorMessage is a short tag like "quota exceeded"; the full
        // row renders as "Failed (quota exceeded)". Plain "Failed"
        // when nothing specific is known. failureReason (D.2) is the
        // typed streaming variant — prefer it when set, otherwise fall
        // back to the existing errorMessage path for classic failures.
        TransferStatus.FAILED -> (transfer.failureReason ?: transfer.errorMessage)
            ?.let { "Failed ($it)" } ?: "Failed"
        // --- Streaming labels (wiring lands in D.3/D.4a/D.4b). ---
        // D.5 will refine the SENDING / WAITING_STREAM cases into the
        // full two-metric rendering. This branch covers the post-
        // delivery "Delivered" case by checking `delivered` — the
        // tracker writes that flag via markDelivered once the server
        // reports delivery_state == "delivered", but it does NOT flip
        // the status itself (streaming rows stay in SENDING post-
        // delivery, consistent with classic rows staying in COMPLETE).
        TransferStatus.SENDING -> when {
            transfer.delivered -> "Delivered"
            transfer.deliveryTotal > 0 -> "Sending ${transfer.chunksUploaded}→${transfer.deliveryChunks}"
            transfer.totalChunks > 0 -> "Sending ${transfer.chunksUploaded}/${transfer.totalChunks}"
            else -> "Sending"
        }
        TransferStatus.WAITING_STREAM -> if (transfer.deliveryTotal > 0)
            "Waiting ${transfer.chunksUploaded}→${transfer.deliveryChunks}"
        else "Waiting"
        TransferStatus.DELIVERING -> if (transfer.deliveryTotal > 0)
            "Delivering ${transfer.deliveryChunks}/${transfer.deliveryTotal}"
        else "Delivering"
        TransferStatus.ABORTED -> transfer.abortReason?.let { reason ->
            when (reason) {
                "sender_abort" -> "Aborted (sender cancelled)"
                "sender_failed" -> "Aborted (sender gave up)"
                "recipient_abort" -> "Aborted (recipient cancelled)"
                else -> "Aborted ($reason)"
            }
        } ?: "Aborted"
    }
    val label = transfer.displayLabel.ifEmpty { transfer.displayName }
    val isClipboard = transfer.displayName.startsWith(".fn.clipboard")
    val isLink = isClipboard && containsSingleUrl(label)
    val mime = transfer.mimeType

    Card(
        onClick = onClick,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            // Icon / thumbnail
            Box(
                modifier = Modifier
                    .size(44.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant),
                contentAlignment = Alignment.Center,
            ) {
                if (isLink) {
                    Icon(
                        painter = androidx.compose.ui.res.painterResource(R.drawable.ic_link),
                        contentDescription = null,
                        modifier = Modifier.size(24.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else if (isClipboard) {
                    Icon(
                        painter = androidx.compose.ui.res.painterResource(R.drawable.ic_clipboard),
                        contentDescription = null,
                        modifier = Modifier.size(24.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else if (mime.startsWith("image/") || mime.startsWith("video/")) {
                    TransferThumbnail(transfer)
                } else if (mime == "application/vnd.android.package-archive" || transfer.displayName.endsWith(".apk")) {
                    Icon(
                        painter = androidx.compose.ui.res.painterResource(R.drawable.ic_apk),
                        contentDescription = null,
                        modifier = Modifier.size(28.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else {
                    Icon(
                        imageVector = if (mime.startsWith("text/")) Icons.Default.Edit
                            else Icons.Default.Share,
                        contentDescription = null,
                        modifier = Modifier.size(24.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            Spacer(Modifier.width(10.dp))
            // Text content
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    dirLabel + label,
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.height(2.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        formatSize(transfer.sizeBytes),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(
                        statusText,
                        style = MaterialTheme.typography.labelMedium,
                        color = statusColor,
                    )
                }
            }
        }
        // Progress bars. Colour per phase:
        //   UPLOADING outgoing / WAITING_STREAM  → transferOutgoing (yellow)
        //   UPLOADING incoming                   → transferIncoming (sky blue)
        //   SENDING / DELIVERING / COMPLETE-delivering → transferDelivering (blue)
        //
        // Streaming SENDING prefers the delivery fraction (Y/N) when
        // the tracker has painted `deliveryTotal`. Falls back to the
        // upload fraction while the first chunk is still making its
        // way to the recipient (tracker hasn't ticked yet).
        val barModifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp)
            .padding(bottom = 6.dp)
        when {
            transfer.status == TransferStatus.UPLOADING && transfer.totalChunks > 0 -> {
                val barColor = if (transfer.direction == TransferDirection.INCOMING) brand.transferIncoming else brand.transferOutgoing
                LinearProgressIndicator(
                    progress = { transfer.chunksUploaded.toFloat() / transfer.totalChunks },
                    modifier = barModifier,
                    color = barColor,
                )
            }
            transfer.status == TransferStatus.WAITING_STREAM && transfer.totalChunks > 0 -> {
                // Yellow bar pinned at the current upload cursor. The
                // "stalled" feeling comes from the fraction not advancing
                // — simpler than a pulse animation and still obvious.
                LinearProgressIndicator(
                    progress = { transfer.chunksUploaded.toFloat() / transfer.totalChunks },
                    modifier = barModifier,
                    color = brand.transferOutgoing,
                )
            }
            transfer.status == TransferStatus.SENDING && !transfer.delivered -> {
                val fraction = when {
                    transfer.deliveryTotal > 0 ->
                        transfer.deliveryChunks.toFloat() / transfer.deliveryTotal
                    transfer.totalChunks > 0 ->
                        transfer.chunksUploaded.toFloat() / transfer.totalChunks
                    else -> 0f
                }
                LinearProgressIndicator(
                    progress = { fraction },
                    modifier = barModifier,
                    color = brand.transferDelivering,
                )
            }
            transfer.status == TransferStatus.DELIVERING && transfer.deliveryTotal > 0 -> {
                // Reserved — no Android writer produces DELIVERING in
                // the current sender state machine (streaming rows stay
                // SENDING until delivered=1). Render for parity with
                // desktop so an out-of-band writer doesn't produce a
                // blank row.
                LinearProgressIndicator(
                    progress = { transfer.deliveryChunks.toFloat() / transfer.deliveryTotal },
                    modifier = barModifier,
                    color = brand.transferDelivering,
                )
            }
            transfer.status == TransferStatus.COMPLETE
                && transfer.direction == TransferDirection.OUTGOING
                && !transfer.delivered
                && transfer.deliveryTotal > 0 -> {
                LinearProgressIndicator(
                    progress = { transfer.deliveryChunks.toFloat() / transfer.deliveryTotal },
                    modifier = barModifier,
                    color = brand.transferDelivering,
                )
            }
        }
    }
}

@Composable
private fun TransferThumbnail(transfer: QueuedTransfer) {
    val context = LocalContext.current
    val bitmap = remember(transfer.contentUri) {
        if (transfer.contentUri.isEmpty()) return@remember null
        try {
            val uri = Uri.parse(transfer.contentUri)
            if (uri.scheme == "file") {
                // Local file — decode a downsampled bitmap directly
                val path = uri.path ?: return@remember null
                val opts = android.graphics.BitmapFactory.Options().apply {
                    inJustDecodeBounds = true
                }
                android.graphics.BitmapFactory.decodeFile(path, opts)
                opts.inSampleSize = maxOf(1, maxOf(opts.outWidth, opts.outHeight) / 128)
                opts.inJustDecodeBounds = false
                android.graphics.BitmapFactory.decodeFile(path, opts)
            } else {
                // Content URI — use loadThumbnail
                context.contentResolver.loadThumbnail(uri, android.util.Size(128, 128), null)
            }
        } catch (_: Exception) { null }
    }
    if (bitmap != null) {
        androidx.compose.foundation.Image(
            bitmap = bitmap.asImageBitmap(),
            contentDescription = null,
            contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxSize(),
        )
    } else {
        Icon(
            imageVector = Icons.Default.Share,
            contentDescription = null,
            modifier = Modifier.size(24.dp),
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun SwipeToDeleteItem(
    onDelete: () -> Unit,
    content: @Composable () -> Unit,
) {
    var dismissed by remember { mutableStateOf(false) }
    val dismissState = rememberSwipeToDismissBoxState(
        confirmValueChange = { value ->
            if (value != SwipeToDismissBoxValue.Settled) {
                dismissed = true
                true
            } else false
        }
    )

    androidx.compose.animation.AnimatedVisibility(
        visible = !dismissed,
        exit = androidx.compose.animation.shrinkVertically(
            animationSpec = androidx.compose.animation.core.tween(250),
        ) + androidx.compose.animation.fadeOut(
            animationSpec = androidx.compose.animation.core.tween(150),
        ),
    ) {
        val errorColor = MaterialTheme.colorScheme.error
        SwipeToDismissBox(
            state = dismissState,
            backgroundContent = {
                val color by animateColorAsState(
                    when (dismissState.targetValue) {
                        SwipeToDismissBoxValue.Settled -> Color.Transparent
                        else -> errorColor
                    },
                    label = "swipe-bg",
                )
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .background(color, RoundedCornerShape(12.dp))
                        .padding(horizontal = 20.dp),
                    contentAlignment = when (dismissState.dismissDirection) {
                        SwipeToDismissBoxValue.StartToEnd -> Alignment.CenterStart
                        else -> Alignment.CenterEnd
                    },
                ) {
                    Icon(Icons.Default.Delete, "Delete", tint = Color.White)
                }
            },
        ) {
            content()
        }
    }

    // Trigger actual deletion after animation completes
    LaunchedEffect(dismissed) {
        if (dismissed) {
            kotlinx.coroutines.delay(280)
            onDelete()
        }
    }
}

private data class RecentFile(
    val uri: Uri,
    val name: String,
    val size: Long,
    val hasThumb: Boolean,
    val mimeType: String,
    val dateModified: Long,
)

private class RecentFilesLoader(private val context: android.content.Context) {
    private val PAGE_SIZE = 15
    private var offset = 0
    private var exhausted = false
    val files = mutableStateListOf<RecentFile>()

    init { loadMore() }

    fun refresh() {
        files.clear()
        offset = 0
        exhausted = false
        loadMore()
    }

    fun loadMore() {
        if (exhausted) return
        try {
            val projection = arrayOf(
                MediaStore.Files.FileColumns._ID,
                MediaStore.Files.FileColumns.DISPLAY_NAME,
                MediaStore.Files.FileColumns.SIZE,
                MediaStore.Files.FileColumns.MIME_TYPE,
                MediaStore.Files.FileColumns.DATE_MODIFIED,
                MediaStore.Files.FileColumns.RELATIVE_PATH,
            )
            val selection = "${MediaStore.Files.FileColumns.SIZE} > 0"
            val sortOrder = "${MediaStore.Files.FileColumns.DATE_MODIFIED} DESC"

            context.contentResolver.query(
                MediaStore.Files.getContentUri("external"),
                projection, selection, null, sortOrder,
            )?.use { cursor ->
                val idCol = cursor.getColumnIndexOrThrow(MediaStore.Files.FileColumns._ID)
                val nameCol = cursor.getColumnIndexOrThrow(MediaStore.Files.FileColumns.DISPLAY_NAME)
                val sizeCol = cursor.getColumnIndexOrThrow(MediaStore.Files.FileColumns.SIZE)
                val mimeCol = cursor.getColumnIndexOrThrow(MediaStore.Files.FileColumns.MIME_TYPE)
                val dateCol = cursor.getColumnIndexOrThrow(MediaStore.Files.FileColumns.DATE_MODIFIED)
                val pathCol = cursor.getColumnIndexOrThrow(MediaStore.Files.FileColumns.RELATIVE_PATH)

                // Skip to offset
                var skipped = 0
                while (skipped < offset && cursor.moveToNext()) skipped++

                var added = 0
                while (cursor.moveToNext() && added < PAGE_SIZE) {
                    val path = cursor.getString(pathCol) ?: ""
                    // Filter out files from DesktopConnector folder
                    if (path.startsWith("DesktopConnector")) continue

                    val id = cursor.getLong(idCol)
                    val name = cursor.getString(nameCol) ?: continue
                    val size = cursor.getLong(sizeCol)
                    val mime = cursor.getString(mimeCol) ?: ""
                    val date = cursor.getLong(dateCol)
                    val uri = ContentUris.withAppendedId(MediaStore.Files.getContentUri("external"), id)
                    val hasThumb = mime.startsWith("image/") || mime.startsWith("video/")
                    files.add(RecentFile(uri, name, size, hasThumb, mime, date))
                    added++
                }
                offset = skipped + added + (offset - skipped).coerceAtLeast(0)
                if (added < PAGE_SIZE) exhausted = true
            }
        } catch (_: Exception) {
            exhausted = true
        }
    }
}

@Composable
private fun rememberRecentFilesLoader(): RecentFilesLoader {
    val context = LocalContext.current
    return remember { RecentFilesLoader(context) }
}

@Composable
private fun RecentFilesStrip(
    loader: RecentFilesLoader,
    onSend: (Uri) -> Unit,
) {
    if (loader.files.isEmpty()) return

    var selectedFile by remember { mutableStateOf<RecentFile?>(null) }

    Column(modifier = Modifier.padding(top = 8.dp)) {
        Text(
            "Recent files",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
        )
        val listState = rememberLazyListState()
        LazyRow(
            state = listState,
            contentPadding = PaddingValues(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.height(130.dp),
        ) {
            items(loader.files.size) { index ->
                val file = loader.files[index]
                RecentFileTile(file, onClick = { selectedFile = file })
            }
        }

        // Load more when approaching end
        val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
        LaunchedEffect(lastVisible) {
            if (lastVisible >= loader.files.size - 5) {
                loader.loadMore()
            }
        }
    }

    // Detail modal
    selectedFile?.let { file ->
        RecentFileDialog(
            file = file,
            onSend = {
                onSend(file.uri)
                selectedFile = null
            },
            onDismiss = { selectedFile = null },
        )
    }
}

@Composable
private fun RecentFileTile(file: RecentFile, onClick: () -> Unit) {
    Card(
        onClick = onClick,
        modifier = Modifier
            .width(100.dp)
            .fillMaxHeight(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        Column(
            modifier = Modifier.fillMaxSize(),
        ) {
            // Thumbnail area — fixed height
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(60.dp)
                    .clip(RoundedCornerShape(topStart = 12.dp, topEnd = 12.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant),
                contentAlignment = Alignment.Center,
            ) {
                if (file.hasThumb) {
                    val context = LocalContext.current
                    val bitmap = remember(file.uri) {
                        try {
                            context.contentResolver.loadThumbnail(
                                file.uri, android.util.Size(200, 200), null
                            )
                        } catch (_: Exception) { null }
                    }
                    if (bitmap != null) {
                        androidx.compose.foundation.Image(
                            bitmap = bitmap!!.asImageBitmap(),
                            contentDescription = file.name,
                            contentScale = ContentScale.Crop,
                            modifier = Modifier.fillMaxSize(),
                        )
                    } else {
                        Icon(Icons.Default.Share, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                } else {
                    Icon(Icons.Default.Share, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            // Text area — fills remaining space
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f)
                    .padding(horizontal = 6.dp, vertical = 4.dp),
                verticalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    file.name,
                    style = MaterialTheme.typography.labelSmall,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    lineHeight = MaterialTheme.typography.labelSmall.fontSize * 1.1,
                )
                Text(
                    formatSize(file.size),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RecentFileDialog(
    file: RecentFile,
    onSend: () -> Unit,
    onDismiss: () -> Unit,
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    val context = LocalContext.current

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = MaterialTheme.colorScheme.surface,
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 24.dp)
                .padding(bottom = 32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(280.dp)
                    .clip(RoundedCornerShape(16.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .clickable { openUriExternally(context, file.uri, file.mimeType) },
                contentAlignment = Alignment.Center,
            ) {
                if (file.hasThumb) {
                    val bitmap = remember(file.uri) {
                        try {
                            context.contentResolver.loadThumbnail(
                                file.uri, android.util.Size(800, 800), null
                            )
                        } catch (_: Exception) { null }
                    }
                    if (bitmap != null) {
                        androidx.compose.foundation.Image(
                            bitmap = bitmap!!.asImageBitmap(),
                            contentDescription = file.name,
                            contentScale = ContentScale.Crop,
                            modifier = Modifier.fillMaxSize(),
                        )
                    } else {
                        Icon(Icons.Default.Share, null,
                            modifier = Modifier.size(48.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                } else {
                    Icon(Icons.Default.Share, null,
                        modifier = Modifier.size(48.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }

            Spacer(Modifier.height(16.dp))

            Text(
                file.name,
                style = MaterialTheme.typography.titleMedium,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "${formatSize(file.size)}  \u00b7  ${formatTimestamp(file.dateModified)}",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            Spacer(Modifier.height(24.dp))

            Button(
                onClick = onSend,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Icon(Icons.AutoMirrored.Filled.Send, null, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(8.dp))
                Text("Send to Desktop", style = MaterialTheme.typography.titleSmall)
            }
        }
    }
}

private fun formatTimestamp(epochSeconds: Long): String {
    val sdf = java.text.SimpleDateFormat("MMM d, HH:mm", java.util.Locale.getDefault())
    return sdf.format(java.util.Date(epochSeconds * 1000))
}

private val URL_REGEX = Regex("https?://\\S+")

/** Returns true if text contains exactly one URL. */
fun containsSingleUrl(text: String): Boolean {
    val matches = URL_REGEX.findAll(text).toList()
    return matches.size == 1
}

/** Extract the single URL from text, or null. */
fun extractSingleUrl(text: String): String? {
    val matches = URL_REGEX.findAll(text).toList()
    return if (matches.size == 1) matches[0].value else null
}

private fun formatSize(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    if (bytes < 1024 * 1024) return "${bytes / 1024} KB"
    return "${"%.1f".format(bytes / (1024.0 * 1024.0))} MB"
}
