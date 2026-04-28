package com.desktopconnector.ui.update

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.util.Locale

@Composable
fun UpdateModal(
    state: UpdateUiState,
    open: Boolean,
    onInstall: () -> Unit,
    onSkip: () -> Unit,
    onDismiss: () -> Unit,
    onCancelDownload: () -> Unit,
) {
    if (!open) return
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(titleFor(state)) },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                BodyContent(state)
            }
        },
        confirmButton = { ConfirmButton(state, onInstall, onCancelDownload, onDismiss) },
        dismissButton = { DismissButton(state, onSkip) },
    )
}

private fun titleFor(state: UpdateUiState): String = when (state) {
    UpdateUiState.Idle -> "Check for updates"
    is UpdateUiState.Checking -> "Checking for updates"
    is UpdateUiState.NoUpdate -> "You're up to date"
    is UpdateUiState.Available -> "Update available"
    is UpdateUiState.Downloading -> "Downloading update"
    is UpdateUiState.Launching -> "Opening installer"
    is UpdateUiState.Error -> "Couldn't check for updates"
}

@Composable
private fun BodyContent(state: UpdateUiState) {
    when (state) {
        UpdateUiState.Idle ->
            Text("Tap Check to see if a new version is available.")
        is UpdateUiState.Checking ->
            CircularProgressIndicator()
        is UpdateUiState.NoUpdate ->
            Text("You're on the latest version (v${state.currentVersion}).")
        is UpdateUiState.Available -> {
            Text(
                "Current: v${state.info.currentVersion}\nLatest: v${state.info.latestVersion}",
                style = MaterialTheme.typography.bodyMedium,
            )
            if (state.info.releaseNotes.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    state.info.releaseNotes.trim().take(800),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        is UpdateUiState.Downloading -> {
            val pct = (state.progress * 100).toInt().coerceIn(0, 100)
            Text("$pct%  (${formatBytes(state.bytesRead)} / ${formatBytes(state.total)})")
            Spacer(Modifier.height(8.dp))
            if (state.total > 0) {
                LinearProgressIndicator(
                    progress = { state.progress.coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth(),
                )
            } else {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            }
        }
        is UpdateUiState.Launching -> {
            CircularProgressIndicator()
            Text("Opening installer…")
        }
        is UpdateUiState.Error ->
            Text(state.message, color = MaterialTheme.colorScheme.error)
    }
}

@Composable
private fun ConfirmButton(
    state: UpdateUiState,
    onInstall: () -> Unit,
    onCancelDownload: () -> Unit,
    onDismiss: () -> Unit,
) {
    when (state) {
        is UpdateUiState.Available ->
            TextButton(onClick = onInstall) { Text("Install") }
        is UpdateUiState.Downloading ->
            TextButton(onClick = onCancelDownload) { Text("Cancel") }
        UpdateUiState.Idle,
        is UpdateUiState.NoUpdate,
        is UpdateUiState.Error ->
            TextButton(onClick = onDismiss) { Text("Close") }
        is UpdateUiState.Checking,
        is UpdateUiState.Launching -> {
            // Transient state — modal closes itself; no primary action.
        }
    }
}

@Composable
private fun DismissButton(state: UpdateUiState, onSkip: () -> Unit) {
    if (state is UpdateUiState.Available) {
        if (state.dismissed) {
            TextButton(onClick = {}, enabled = false) { Text("Skipped") }
        } else {
            TextButton(onClick = onSkip) { Text("Skip this version") }
        }
    }
}

private fun formatBytes(bytes: Long): String {
    if (bytes <= 0) return "—"
    if (bytes < 1024) return "$bytes B"
    val kb = bytes / 1024.0
    if (kb < 1024) return String.format(Locale.US, "%.1f KB", kb)
    val mb = kb / 1024.0
    return String.format(Locale.US, "%.1f MB", mb)
}
