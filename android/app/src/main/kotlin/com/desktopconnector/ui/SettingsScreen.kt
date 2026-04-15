package com.desktopconnector.ui

import androidx.compose.foundation.layout.*
import androidx.compose.ui.Alignment
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.network.ApiClient
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
    onSendLogs: (String) -> Unit,
    onBack: () -> Unit,
) {
    var serverUrl by remember { mutableStateOf(prefs.serverUrl ?: "") }
    var stats by remember { mutableStateOf<JSONObject?>(null) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        withContext(Dispatchers.IO) {
            if (prefs.serverUrl != null && prefs.deviceId != null && prefs.authToken != null) {
                val api = ApiClient(prefs.serverUrl!!, prefs.deviceId!!, prefs.authToken!!)
                stats = api.getStats()
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
                            SettingsRow("Status", if (online) "Online" else "Offline")
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
                Column(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                    Text("Logs", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = { AppLog.clear() }) {
                            Text("Clear")
                        }
                        OutlinedButton(onClick = {
                            val text = AppLog.read()
                            if (text.isNotEmpty()) {
                                onSendLogs(text)
                            }
                        }) {
                            Text("Send to Desktop")
                        }
                    }
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
