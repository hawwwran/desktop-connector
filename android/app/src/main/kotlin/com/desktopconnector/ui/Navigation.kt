package com.desktopconnector.ui

import android.net.Uri
import androidx.compose.runtime.*
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.ui.pairing.PairingScreen
import com.desktopconnector.ui.pairing.PairingStage
import com.desktopconnector.ui.pairing.PairingViewModel
import com.desktopconnector.ui.transfer.TransferViewModel

@Composable
fun AppNavigation(
    prefs: AppPreferences,
    keyManager: KeyManager,
    initialUris: List<Uri> = emptyList(),
    initialClipboardText: String? = null,
) {
    val navController = rememberNavController()
    val startDest = if (keyManager.hasPairedDevice()) "home" else "pairing"

    val transferViewModel: TransferViewModel = viewModel()
    val pairingViewModel: PairingViewModel = viewModel()

    // Handle initial share intent URIs
    LaunchedEffect(initialUris) {
        if (initialUris.isNotEmpty() && keyManager.hasPairedDevice()) {
            transferViewModel.queueFiles(initialUris)
        }
    }

    // Handle shared text (URLs from Chrome, YouTube, etc.)
    LaunchedEffect(initialClipboardText) {
        if (initialClipboardText != null && keyManager.hasPairedDevice()) {
            transferViewModel.sendClipboardText(initialClipboardText)
        }
    }

    // React to remote unpair — navigate to pairing only on true→false transition
    val isPaired by transferViewModel.isPaired.collectAsState()
    var wasPaired by remember { mutableStateOf(isPaired) }
    LaunchedEffect(isPaired) {
        if (wasPaired && !isPaired) {
            navController.navigate("pairing") {
                popUpTo(0) { inclusive = true }
            }
        }
        wasPaired = isPaired
    }

    NavHost(navController = navController, startDestination = startDest) {
        composable("home") {
            val connState by transferViewModel.connectionState.collectAsState()
            val transfers by transferViewModel.transfers.collectAsState()
            val isRefreshing by transferViewModel.isRefreshing.collectAsState()
            val linkDialog by transferViewModel.linkDialog.collectAsState()

            // Link open/copy dialog
            linkDialog?.let { (url, fullText) ->
                androidx.compose.material3.AlertDialog(
                    onDismissRequest = { transferViewModel.dismissLinkDialog() },
                    title = { androidx.compose.material3.Text("Link detected") },
                    text = { androidx.compose.material3.Text(url, maxLines = 3) },
                    confirmButton = {
                        androidx.compose.material3.TextButton(
                            onClick = { transferViewModel.openLink(url) }
                        ) { androidx.compose.material3.Text("Open") }
                    },
                    dismissButton = {
                        androidx.compose.material3.TextButton(
                            onClick = { transferViewModel.copyLinkToClipboard(fullText) }
                        ) { androidx.compose.material3.Text("Copy") }
                    },
                )
            }

            HomeScreen(
                connectionState = connState,
                transfers = transfers,
                isRefreshing = isRefreshing,
                onFilesSelected = { uris -> transferViewModel.queueFiles(uris) },
                onSendClipboard = { transferViewModel.sendClipboard() },
                onSendUri = { uri -> transferViewModel.queueFiles(listOf(uri)) },
                onItemClick = { transfer -> transferViewModel.onItemClick(transfer) },
                onDelete = { transfer -> transferViewModel.deleteTransfer(transfer) },
                onRefresh = { transferViewModel.onRefresh() },
                onNavigateSettings = { navController.navigate("settings") },
            )
        }

        composable("pairing") {
            // Reset pairing state every time we enter this screen
            LaunchedEffect(Unit) {
                pairingViewModel.reset()
            }

            val state by pairingViewModel.state.collectAsState()

            LaunchedEffect(state.stage) {
                if (state.stage == PairingStage.COMPLETE) {
                    navController.navigate("home") {
                        popUpTo("pairing") { inclusive = true }
                    }
                }
            }

            PairingScreen(
                onQrScanned = { server, id, pubkey, name ->
                    pairingViewModel.onQrScanned(server, id, pubkey, name)
                },
                verificationCode = state.verificationCode,
                onConfirmPairing = { pairingViewModel.confirmPairing() },
                onCancel = { pairingViewModel.cancel() },
            )
        }

        composable("settings") {
            val paired = keyManager.getFirstPairedDevice()
            SettingsScreen(
                prefs = prefs,
                deviceId = keyManager.deviceId,
                pairedDeviceName = paired?.name ?: "",
                pairedDeviceId = paired?.deviceId ?: "",
                onUnpair = {
                    paired?.let {
                        // Notify desktop before removing pairing
                        transferViewModel.sendUnpairNotification(it.deviceId)
                        keyManager.removePairedDevice(it.deviceId)
                    }
                    navController.navigate("pairing") {
                        popUpTo("home") { inclusive = true }
                    }
                },
                onSendLogs = { text ->
                    // Send logs as a file transfer
                    transferViewModel.sendLogsToDesktop(text)
                },
                onBack = { navController.popBackStack() },
            )
        }
    }
}
