package com.desktopconnector.ui

import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaScannerConnection
import android.os.Environment
import android.webkit.MimeTypeMap
import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.foundation.background
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private data class FolderFile(
    val file: File,
    val name: String,
    val size: Long,
    val modified: Long,
    val mimeType: String,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FolderScreen(
    onBack: () -> Unit,
) {
    val context = LocalContext.current
    val dir = remember {
        File(Environment.getExternalStorageDirectory(), "DesktopConnector")
    }

    var files by remember { mutableStateOf(loadFiles(dir)) }
    var totalSize by remember { mutableStateOf(files.sumOf { it.size }) }
    var showDeleteAllDialog by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    fun refresh() {
        files = loadFiles(dir)
        totalSize = files.sumOf { it.size }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Downloads")
                        Text(
                            "${files.size} files \u00b7 ${formatBytes(totalSize)}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back")
                    }
                },
                actions = {
                    if (files.isNotEmpty()) {
                        IconButton(onClick = { showDeleteAllDialog = true }) {
                            Icon(Icons.Default.Delete, "Delete all")
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                ),
            )
        },
    ) { padding ->
        if (showDeleteAllDialog) {
            AlertDialog(
                onDismissRequest = { showDeleteAllDialog = false },
                title = { Text("Delete all files?") },
                text = { Text("This will permanently delete ${files.size} files (${formatBytes(totalSize)}) from the DesktopConnector folder.") },
                confirmButton = {
                    TextButton(onClick = {
                        showDeleteAllDialog = false
                        val toDelete = files.toList()
                        files = emptyList()
                        totalSize = 0
                        scope.launch(Dispatchers.IO) {
                            for (f in toDelete) {
                                f.file.delete()
                                MediaScannerConnection.scanFile(context, arrayOf(f.file.absolutePath), null, null)
                            }
                        }
                    }) { Text("Delete All", color = MaterialTheme.colorScheme.error) }
                },
                dismissButton = {
                    TextButton(onClick = { showDeleteAllDialog = false }) { Text("Cancel") }
                },
            )
        }

        if (files.isEmpty()) {
            Box(
                modifier = Modifier
                    .padding(padding)
                    .fillMaxSize(),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    "No files",
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier
                    .padding(padding)
                    .fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                items(files, key = { it.file.absolutePath }) { item ->
                    SwipeToDeleteItem(
                        onDelete = {
                            files = files.filter { it.file.absolutePath != item.file.absolutePath }
                            totalSize = files.sumOf { it.size }
                            scope.launch(Dispatchers.IO) {
                                item.file.delete()
                                MediaScannerConnection.scanFile(context, arrayOf(item.file.absolutePath), null, null)
                            }
                        },
                    ) {
                        FileCard(item) {
                            try {
                                val uri = FileProvider.getUriForFile(
                                    context, "${context.packageName}.fileprovider", item.file
                                )
                                val intent = Intent(Intent.ACTION_VIEW).apply {
                                    setDataAndType(uri, item.mimeType)
                                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                                }
                                context.startActivity(intent)
                            } catch (_: ActivityNotFoundException) {
                                Toast.makeText(context, "No app to open this file", Toast.LENGTH_SHORT).show()
                            } catch (_: Exception) {
                                Toast.makeText(context, "Cannot open file", Toast.LENGTH_SHORT).show()
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun FileCard(item: FolderFile, onClick: () -> Unit) {
    val dateFormat = remember { SimpleDateFormat("MMM d, HH:mm", Locale.getDefault()) }

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
            // Thumbnail — matches history item style (44dp, rounded, surfaceVariant bg)
            Box(
                modifier = Modifier
                    .size(44.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant),
                contentAlignment = Alignment.Center,
            ) {
                val mime = item.mimeType
                if (mime.startsWith("image/") || mime.startsWith("video/")) {
                    var bitmap by remember { mutableStateOf<Bitmap?>(null) }
                    LaunchedEffect(item.file.absolutePath) {
                        bitmap = withContext(Dispatchers.IO) {
                            try {
                                val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                                BitmapFactory.decodeFile(item.file.absolutePath, opts)
                                val scale = maxOf(1, maxOf(opts.outWidth, opts.outHeight) / 128)
                                BitmapFactory.decodeFile(item.file.absolutePath, BitmapFactory.Options().apply {
                                    inSampleSize = scale
                                    inJustDecodeBounds = false
                                })
                            } catch (_: Exception) { null }
                        }
                    }
                    val bmp = bitmap
                    if (bmp != null) {
                        androidx.compose.foundation.Image(
                            bitmap = bmp.asImageBitmap(),
                            contentDescription = null,
                            modifier = Modifier.fillMaxSize(),
                            contentScale = ContentScale.Crop,
                        )
                    } else {
                        FileIcon()
                    }
                } else {
                    FileIcon()
                }
            }

            Spacer(Modifier.width(10.dp))

            Column(modifier = Modifier.weight(1f)) {
                Text(
                    item.name,
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.height(2.dp))
                Text(
                    "${formatBytes(item.size)} \u00b7 ${dateFormat.format(Date(item.modified))}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun FileIcon() {
    Icon(
        imageVector = Icons.Default.Share,
        contentDescription = null,
        modifier = Modifier.size(24.dp),
        tint = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

private fun loadFiles(dir: File): List<FolderFile> {
    if (!dir.exists()) return emptyList()
    return dir.listFiles()
        ?.filter { it.isFile }
        ?.sortedByDescending { it.lastModified() }
        ?.map { file ->
            val ext = file.extension.lowercase()
            val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext)
                ?: "application/octet-stream"
            FolderFile(
                file = file,
                name = file.name,
                size = file.length(),
                modified = file.lastModified(),
                mimeType = mime,
            )
        } ?: emptyList()
}

private fun formatBytes(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    if (bytes < 1024 * 1024) return "${bytes / 1024} KB"
    if (bytes < 1024 * 1024 * 1024) return "${"%.1f".format(bytes / (1024.0 * 1024.0))} MB"
    return "${"%.2f".format(bytes / (1024.0 * 1024.0 * 1024.0))} GB"
}
