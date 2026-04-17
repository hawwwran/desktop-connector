package com.desktopconnector.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.ui.Alignment
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.network.ApiClient
import com.desktopconnector.network.FcmManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    prefs: AppPreferences,
    deviceId: String,
    pairedDeviceName: String,
    pairedDeviceId: String,
    onUnpair: () -> Unit,
    onSendLogs: (String, Boolean) -> Unit,
    onDownloadLogs: (String, Boolean) -> Unit,
    onBack: () -> Unit,
) {
    var serverUrl by remember { mutableStateOf(prefs.serverUrl ?: "") }
    var stats by remember { mutableStateOf<JSONObject?>(null) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        withContext(Dispatchers.IO) {
            if (prefs.serverUrl != null && prefs.deviceId != null && prefs.authToken != null) {
                val api = ApiClient(prefs.serverUrl!!, prefs.deviceId!!, prefs.authToken!!)
                stats = api.getStats(pairedWith = pairedDeviceId.ifEmpty { null })
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                ),
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .padding(16.dp)
                .fillMaxSize()
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            // Server URL
            OutlinedTextField(
                value = serverUrl,
                onValueChange = {
                    serverUrl = it
                    prefs.serverUrl = it
                },
                label = { Text("Server URL") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
            )

            // Long poll status
            val lpStatus = com.desktopconnector.service.PollService.longPollStatus
            val lpLabel = when (lpStatus) {
                "active" -> "Active"
                "unavailable" -> "Not available"
                "testing" -> "Testing (may take up to 35s)..."
                "offline" -> "Offline"
                else -> "Unknown"
            }
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            ) {
                Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                    Text("Long Polling", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(4.dp))
                    SettingsRow("Status", lpLabel)
                    if (lpStatus != "active") {
                        Spacer(Modifier.height(8.dp))
                        OutlinedButton(onClick = {
                            com.desktopconnector.service.PollService.retryLongPoll = true
                        }) {
                            Text("Retry")
                        }
                    }
                }
            }

            // FCM push status
            val context = LocalContext.current
            var fcmChecking by remember { mutableStateOf(false) }
            var fcmActive by remember { mutableStateOf(FcmManager.isInitialized) }
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            ) {
                Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                    Text("FCM Push Wake", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(4.dp))
                    SettingsRow("Status", if (fcmChecking) "Checking..." else if (fcmActive) "Active" else "Not available")
                    if (!fcmActive) {
                        Spacer(Modifier.height(8.dp))
                        OutlinedButton(
                            onClick = {
                                fcmChecking = true
                                scope.launch(Dispatchers.IO) {
                                    FcmManager.reset()
                                    val result = try {
                                        FcmManager.initialize(context.applicationContext, prefs)
                                    } catch (_: Exception) { false }
                                    withContext(Dispatchers.Main) {
                                        fcmActive = result
                                        fcmChecking = false
                                    }
                                }
                            },
                            enabled = !fcmChecking,
                        ) {
                            Text("Check")
                        }
                    }
                }
            }

            // GPS permission for Find my Phone (only shown when FCM is active)
            if (fcmActive) {
                val context = LocalContext.current
                var hasLocation by remember {
                    mutableStateOf(
                        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
                            PackageManager.PERMISSION_GRANTED
                    )
                }
                val locationLauncher = rememberLauncherForActivityResult(
                    ActivityResultContracts.RequestMultiplePermissions()
                ) { results ->
                    hasLocation = results[Manifest.permission.ACCESS_FINE_LOCATION] == true
                }

                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                ) {
                    Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                        Text("Find My Phone", style = MaterialTheme.typography.titleSmall)
                        Spacer(Modifier.height(4.dp))
                        SettingsRow("GPS Permission", if (hasLocation) "Granted" else "Not granted")
                        if (!hasLocation) {
                            Spacer(Modifier.height(8.dp))
                            OutlinedButton(onClick = {
                                locationLauncher.launch(arrayOf(
                                    Manifest.permission.ACCESS_FINE_LOCATION,
                                    Manifest.permission.ACCESS_COARSE_LOCATION,
                                ))
                            }) {
                                Text("Grant GPS Permission")
                            }
                        }
                        Spacer(Modifier.height(8.dp))
                        var allowSilent by remember { mutableStateOf(prefs.allowSilentSearch) }
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text("Allow silent search", style = MaterialTheme.typography.bodySmall)
                            Switch(
                                checked = allowSilent,
                                onCheckedChange = {
                                    allowSilent = it
                                    prefs.allowSilentSearch = it
                                },
                            )
                        }
                    }
                }
            }

            // Battery optimization
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.M) {
                val context = LocalContext.current
                val pm = remember { context.getSystemService(android.os.PowerManager::class.java) }
                var batteryOptimized by remember {
                    mutableStateOf(!pm.isIgnoringBatteryOptimizations(context.packageName))
                }

                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                ) {
                    Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                        Text("Background Downloads", style = MaterialTheme.typography.titleSmall)
                        Spacer(Modifier.height(4.dp))
                        SettingsRow("Battery optimization", if (batteryOptimized) "Restricted" else "Unrestricted")
                        if (batteryOptimized) {
                            Spacer(Modifier.height(8.dp))
                            OutlinedButton(onClick = {
                                @Suppress("BatteryLife")
                                val intent = android.content.Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                                intent.data = android.net.Uri.parse("package:${context.packageName}")
                                context.startActivity(intent)
                                batteryOptimized = false
                            }) {
                                Text("Remove Restriction")
                            }
                        }
                    }
                }
            }

            // This device
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            ) {
                Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                    Text("This Device", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(8.dp))
                    SettingsRow("ID", "${deviceId.take(16)}...")
                }
            }

            // Paired device
            if (pairedDeviceName.isNotEmpty()) {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                ) {
                    Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                        Text("Paired Desktop", style = MaterialTheme.typography.titleSmall)
                        Spacer(Modifier.height(8.dp))
                        SettingsRow("Name", pairedDeviceName)
                        SettingsRow("ID", "${pairedDeviceId.take(16)}...")

                        // Show paired device online status from stats
                        val pairedStats = stats?.optJSONArray("paired_devices")
                            ?.let { arr -> (0 until arr.length()).map { arr.getJSONObject(it) } }
                            ?.firstOrNull { it.optString("device_id").startsWith(pairedDeviceId.take(16)) }

                        if (pairedStats != null) {
                            val online = pairedStats.optBoolean("online", false)
                            val statusStr = if (online) {
                                "Online"
                            } else {
                                val lastSeen = pairedStats.optLong("last_seen", 0)
                                if (lastSeen > 0) {
                                    val ago = (System.currentTimeMillis() / 1000) - lastSeen
                                    when {
                                        ago < 60 -> "Last seen just now"
                                        ago < 3600 -> "Last seen ${ago / 60} min ago"
                                        ago < 86400 -> "Last seen ${ago / 3600}h ago"
                                        else -> "Last seen ${formatTimestamp(lastSeen)}"
                                    }
                                } else "Offline"
                            }
                            SettingsRow("Status", statusStr)
                        }

                        Spacer(Modifier.height(12.dp))
                        OutlinedButton(
                            onClick = onUnpair,
                            colors = ButtonDefaults.outlinedButtonColors(
                                contentColor = MaterialTheme.colorScheme.error,
                            ),
                        ) {
                            Text("Unpair")
                        }
                    }
                }
            }

            // Connection statistics
            if (stats != null) {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                ) {
                    Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                        Text("Connection Statistics", style = MaterialTheme.typography.titleSmall)
                        Spacer(Modifier.height(8.dp))

                        val pairedArr = stats!!.optJSONArray("paired_devices")
                        val paired = pairedArr?.let {
                            if (it.length() > 0) it.getJSONObject(0) else null
                        }

                        if (paired != null) {
                            SettingsRow("Total transfers", "${paired.optInt("transfers", 0)}")
                            SettingsRow("Data transferred", formatBytes(paired.optLong("bytes_transferred", 0)))
                            SettingsRow("Paired since", formatTimestamp(paired.optLong("paired_since", 0)))
                        }

                        SettingsRow("Pending incoming", "${stats!!.optInt("pending_incoming", 0)}")
                        SettingsRow("Pending outgoing", "${stats!!.optInt("pending_outgoing", 0)}")
                    }
                }
            }

            // Logs
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            ) {
                val context = LocalContext.current
                var loggingOn by remember { mutableStateOf(prefs.loggingEnabled) }
                var showLogsDialog by remember { mutableStateOf(false) }

                Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                    Text("Logs", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(8.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text("Allow logging", style = MaterialTheme.typography.bodySmall)
                        Switch(
                            checked = loggingOn,
                            onCheckedChange = {
                                loggingOn = it
                                prefs.loggingEnabled = it
                                AppLog.refreshEnabled(context)
                            },
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = { AppLog.clear() }) {
                            Text("Clear")
                        }
                        OutlinedButton(onClick = { showLogsDialog = true }) {
                            Text("Download Logs")
                        }
                    }
                }

                if (showLogsDialog) {
                    LogsDialog(
                        onDismiss = { showLogsDialog = false },
                        onSendToDesktop = { appendBatteryStats ->
                            val text = AppLog.read()
                            if (text.isNotEmpty()) {
                                onSendLogs(text, appendBatteryStats)
                            }
                            showLogsDialog = false
                        },
                        onDownloadToPhone = { appendBatteryStats ->
                            val text = AppLog.read()
                            if (text.isNotEmpty()) {
                                onDownloadLogs(text, appendBatteryStats)
                            }
                            showLogsDialog = false
                        }
                    )
                }
            }

            // Version
            Text(
                "Desktop Connector v0.1.0",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun LogsDialog(
    onDismiss: () -> Unit,
    onSendToDesktop: (Boolean) -> Unit,
    onDownloadToPhone: (Boolean) -> Unit,
) {
    var appendBatteryStats by remember { mutableStateOf(false) }
    val context = LocalContext.current

    // Check both permissions needed for battery stats
    val hasAllBatteryStatsPermissions = remember {
        context.checkSelfPermission(android.Manifest.permission.DUMP) == PackageManager.PERMISSION_GRANTED &&
        context.checkSelfPermission(android.Manifest.permission.PACKAGE_USAGE_STATS) == PackageManager.PERMISSION_GRANTED
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Download Logs") },
        text = {
            Column {
                Text(
                    "Choose how to save the logs:",
                    style = MaterialTheme.typography.bodyMedium
                )

                // Only show battery stats toggle if both permissions are granted (developer mode)
                if (hasAllBatteryStatsPermissions) {
                    Spacer(Modifier.height(16.dp))

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            "Append battery usage stats",
                            style = MaterialTheme.typography.bodySmall
                        )
                        Switch(
                            checked = appendBatteryStats,
                            onCheckedChange = { appendBatteryStats = it }
                        )
                    }

                    Spacer(Modifier.height(8.dp))
                    Text(
                        "✓ Developer feature enabled",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.primary
                    )
                }

                Spacer(Modifier.height(16.dp))

                // Action buttons - stacked vertically
                Column(
                    modifier = Modifier.fillMaxWidth(),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Button(
                        onClick = { onSendToDesktop(appendBatteryStats) },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("Send to Desktop")
                    }
                    OutlinedButton(
                        onClick = { onDownloadToPhone(appendBatteryStats) },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("Download to Phone")
                    }
                }
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        },
        confirmButton = {
            // Empty - buttons are in the text section now
        }
    )
}

@Composable
private fun SettingsRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(
            label,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            value,
            style = MaterialTheme.typography.bodySmall,
            fontFamily = FontFamily.Monospace,
        )
    }
}

private fun formatBytes(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    if (bytes < 1024 * 1024) return "${bytes / 1024} KB"
    if (bytes < 1024 * 1024 * 1024) return "${"%.1f".format(bytes / (1024.0 * 1024.0))} MB"
    return "${"%.2f".format(bytes / (1024.0 * 1024.0 * 1024.0))} GB"
}

private fun formatTimestamp(ts: Long): String {
    if (ts == 0L) return "Unknown"
    val sdf = java.text.SimpleDateFormat("MMM d, yyyy", java.util.Locale.getDefault())
    return sdf.format(java.util.Date(ts * 1000))
}
