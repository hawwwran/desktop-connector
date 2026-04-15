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
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.PullToRefreshDefaults
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.*
import androidx.compose.runtime.snapshots.SnapshotStateList
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.desktopconnector.R
import com.desktopconnector.data.QueuedTransfer
import com.desktopconnector.data.TransferDirection
import com.desktopconnector.data.TransferStatus
import com.desktopconnector.network.ConnectionState

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    connectionState: ConnectionState,
    transfers: List<QueuedTransfer>,
    isRefreshing: Boolean,
    onFilesSelected: (List<Uri>) -> Unit,
    onSendClipboard: () -> Unit,
    onSendUri: (Uri) -> Unit,
    onItemClick: (QueuedTransfer) -> Unit,
    onDelete: (QueuedTransfer) -> Unit,
    onRefresh: () -> Unit,
    onNavigateSettings: () -> Unit,
) {
    val filePicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        if (uris.isNotEmpty()) onFilesSelected(uris)
    }

    Scaffold(
        topBar = {
            val dotColor = when (connectionState) {
                ConnectionState.CONNECTED -> Color(0xFF22C55E)
                ConnectionState.RECONNECTING -> Color(0xFFF59E0B)
                ConnectionState.DISCONNECTED -> Color(0xFFEF4444)
            }
            TopAppBar(
                title = {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.clickable { onRefresh() },
                    ) {
                        Text("Desktop Connector")
                        Spacer(Modifier.width(8.dp))
                        Box(
                            modifier = Modifier
                                .size(10.dp)
                                .clip(androidx.compose.foundation.shape.CircleShape)
                                .background(dotColor)
                        )
                    }
                },
                actions = {
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
                    color = MaterialTheme.colorScheme.primary,
                    containerColor = MaterialTheme.colorScheme.surface,
                )
            },
        ) {
            Column(modifier = Modifier.fillMaxSize()) {
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

                Text(
                    "History",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 12.dp),
                )

                if (transfers.isEmpty()) {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .weight(1f),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            "No transfers yet.\nSend files or clipboard to get started.",
                            style = MaterialTheme.typography.bodyLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
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
                                onDelete = { onDelete(transfer) },
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
private fun TransferItem(transfer: QueuedTransfer, onClick: () -> Unit) {
    val statusColor = when {
        transfer.status == TransferStatus.QUEUED -> Color(0xFF94A3B8)
        transfer.status == TransferStatus.UPLOADING -> Color(0xFFF59E0B)
        transfer.status == TransferStatus.COMPLETE && transfer.direction == TransferDirection.INCOMING -> Color(0xFF22C55E)
        transfer.status == TransferStatus.COMPLETE && transfer.delivered -> Color(0xFF22C55E)
        transfer.status == TransferStatus.COMPLETE -> Color(0xFF93C5FD)
        transfer.status == TransferStatus.FAILED -> Color(0xFFEF4444)
        else -> Color(0xFF94A3B8)
    }
    val dirLabel = if (transfer.direction == TransferDirection.INCOMING) "\u2193 " else "\u2191 "
    val statusText = when (transfer.status) {
        TransferStatus.QUEUED -> "Queued"
        TransferStatus.UPLOADING -> if (transfer.totalChunks > 0) "Uploading ${transfer.chunksUploaded}/${transfer.totalChunks}" else "Uploading"
        TransferStatus.COMPLETE -> when {
            transfer.direction == TransferDirection.INCOMING -> "Received"
            transfer.delivered -> "Delivered"
            else -> "Sent"
        }
        TransferStatus.FAILED -> transfer.errorMessage ?: "Failed"
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
                        tint = MaterialTheme.colorScheme.primary,
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
                        tint = Color.Unspecified,
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
        if (transfer.status == TransferStatus.UPLOADING && transfer.totalChunks > 0) {
            LinearProgressIndicator(
                progress = { transfer.chunksUploaded.toFloat() / transfer.totalChunks },
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp)
                    .padding(bottom = 6.dp),
            )
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
private fun SwipeToDeleteItem(
    onDelete: () -> Unit,
    content: @Composable () -> Unit,
) {
    val dismissState = rememberSwipeToDismissBoxState(
        confirmValueChange = { value ->
            if (value != SwipeToDismissBoxValue.Settled) {
                onDelete()
                true
            } else false
        }
    )

    SwipeToDismissBox(
        state = dismissState,
        backgroundContent = {
            val color by animateColorAsState(
                when (dismissState.targetValue) {
                    SwipeToDismissBoxValue.Settled -> Color.Transparent
                    else -> Color(0xFFEF4444)
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
            // Large thumbnail
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(280.dp)
                    .clip(RoundedCornerShape(16.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant),
                contentAlignment = Alignment.Center,
            ) {
                if (file.hasThumb) {
                    val context = LocalContext.current
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
                colors = ButtonDefaults.buttonColors(
                    containerColor = Color(0xFF22C55E),
                    contentColor = Color.White,
                ),
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
