package com.desktopconnector.ui

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.DisposableEffect
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.data.ThemeMode
import com.desktopconnector.network.FcmManager
import com.desktopconnector.service.FindPhoneManager
import com.desktopconnector.ui.pairing.PairingScreen
import com.desktopconnector.ui.pairing.PairingStage
import com.desktopconnector.ui.pairing.PairingViewModel
import com.desktopconnector.ui.transfer.TransferViewModel
import com.desktopconnector.ui.update.UpdateBanner
import com.desktopconnector.ui.update.UpdateModal
import com.desktopconnector.ui.update.UpdateViewModel

@Composable
fun AppNavigation(
    prefs: AppPreferences,
    keyManager: KeyManager,
    themeMode: ThemeMode = ThemeMode.SYSTEM,
    onThemeModeChange: (ThemeMode) -> Unit = {},
    initialUris: List<Uri> = emptyList(),
    initialClipboardText: String? = null,
) {
    val navController = rememberNavController()
    val startDest = if (keyManager.hasPairedDevice()) "home" else "pairing"

    val transferViewModel: TransferViewModel = viewModel()
    val pairingViewModel: PairingViewModel = viewModel()
    val updateViewModel: UpdateViewModel = viewModel()
    val updateState by updateViewModel.state.collectAsState()
    val updateModalOpen by updateViewModel.modalOpen.collectAsState()

    // Wire activity-lifecycle hooks to the UpdateViewModel.
    // ON_RESUME → onAppOpen (rate-limited internally to 24 h via the cache)
    // ON_STOP   → cancelInFlightCheck (abort auto-check on minimize; the
    //   download is intentionally untouched — its WAKE_LOCK keeps it alive)
    val updateLifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(updateLifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_RESUME -> updateViewModel.onAppOpen()
                Lifecycle.Event.ON_STOP -> updateViewModel.cancelInFlightCheck()
                else -> Unit
            }
        }
        updateLifecycleOwner.lifecycle.addObserver(observer)
        onDispose { updateLifecycleOwner.lifecycle.removeObserver(observer) }
    }

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

    // Find my Phone overlay — shown on top of everything when alarm is active
    val findPhoneRinging = remember { mutableStateOf(false) }
    val findPhoneContext = LocalContext.current
    LaunchedEffect(Unit) {
        while (true) {
            findPhoneRinging.value = FindPhoneManager.isRinging
            kotlinx.coroutines.delay(500)
        }
    }

    // Background paint covers the area behind the status bar too. With
    // targetSdk=35 the window.statusBarColor is ignored (edge-to-edge is
    // forced), so without this the activity's window background (the
    // light-mode parent theme's white) shows through the status bar
    // when banner-statusBarsPadding pushes Compose content below it.
    Box(modifier = Modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface)) {

    // statusBarsPadding on the wrapping Column pushes the banner into the
    // safe area AND consumes the status-bar inset, so the screens below
    // (whose Scaffolds default to systemBars insets) don't double-pad
    // their TopAppBars.
    Column(modifier = Modifier.fillMaxSize().statusBarsPadding()) {
        UpdateBanner(state = updateState, onClick = updateViewModel::openModal)

    NavHost(navController = navController, startDestination = startDest) {
        composable("home") {
            val connState by transferViewModel.connectionState.collectAsState()
            val transfers by transferViewModel.transfers.collectAsState()
            val isRefreshing by transferViewModel.isRefreshing.collectAsState()
            val linkDialog by transferViewModel.linkDialog.collectAsState()

            // GPS location permission prompt for Find my Phone
            val context = LocalContext.current
            val hasLocationPermission = remember {
                mutableStateOf(
                    ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
                        PackageManager.PERMISSION_GRANTED
                )
            }
            val locationPermissionLauncher = rememberLauncherForActivityResult(
                ActivityResultContracts.RequestMultiplePermissions()
            ) { results ->
                hasLocationPermission.value = results[Manifest.permission.ACCESS_FINE_LOCATION] == true
            }
            var showLocationPrompt by remember { mutableStateOf(false) }
            var showDismissedMessage by remember { mutableStateOf(false) }

            // Show prompt on every app open: FCM active + not granted + not dismissed
            LaunchedEffect(Unit) {
                if (FcmManager.isInitialized
                    && !hasLocationPermission.value
                    && !prefs.locationPromptDismissed
                ) {
                    showLocationPrompt = true
                }
            }

            if (showLocationPrompt) {
                AlertDialog(
                    onDismissRequest = {},
                    title = { Text("GPS Location") },
                    text = { Text("To support \"Find my Phone\" with GPS location, you must grant a permission.") },
                    confirmButton = {
                        TextButton(onClick = {
                            showLocationPrompt = false
                            locationPermissionLauncher.launch(arrayOf(
                                Manifest.permission.ACCESS_FINE_LOCATION,
                                Manifest.permission.ACCESS_COARSE_LOCATION,
                            ))
                        }) { Text("Grant Now") }
                    },
                    dismissButton = {
                        TextButton(onClick = {
                            showLocationPrompt = false
                            prefs.locationPromptDismissed = true
                            showDismissedMessage = true
                        }) { Text("Dismiss") }
                    },
                )
            }

            if (showDismissedMessage) {
                AlertDialog(
                    onDismissRequest = {},
                    text = { Text("You can grant the permission later in Settings.") },
                    confirmButton = {
                        TextButton(onClick = { showDismissedMessage = false }) { Text("OK") }
                    },
                )
            }

            // Background location prompt (for find-phone when screen is off). Android 11+
            // cannot grant this via a runtime dialog — must route through App Info. Only
            // prompts after foreground location is granted, because system denies the
            // background ask outright without it.
            val hasBackgroundLocation = remember {
                mutableStateOf(
                    android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.Q ||
                        ContextCompat.checkSelfPermission(
                            context,
                            Manifest.permission.ACCESS_BACKGROUND_LOCATION,
                        ) == PackageManager.PERMISSION_GRANTED
                )
            }
            val lifecycleOwner = androidx.lifecycle.compose.LocalLifecycleOwner.current
            DisposableEffect(lifecycleOwner) {
                val observer = androidx.lifecycle.LifecycleEventObserver { _, event ->
                    if (event == androidx.lifecycle.Lifecycle.Event.ON_RESUME) {
                        hasLocationPermission.value = ContextCompat.checkSelfPermission(
                            context, Manifest.permission.ACCESS_FINE_LOCATION
                        ) == PackageManager.PERMISSION_GRANTED
                        hasBackgroundLocation.value =
                            android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.Q ||
                                ContextCompat.checkSelfPermission(
                                    context,
                                    Manifest.permission.ACCESS_BACKGROUND_LOCATION,
                                ) == PackageManager.PERMISSION_GRANTED
                    }
                }
                lifecycleOwner.lifecycle.addObserver(observer)
                onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
            }

            var showBackgroundPrompt by remember { mutableStateOf(false) }
            var showBackgroundDismissed by remember { mutableStateOf(false) }

            LaunchedEffect(hasLocationPermission.value) {
                if (FcmManager.isInitialized
                    && hasLocationPermission.value
                    && !hasBackgroundLocation.value
                    && !prefs.backgroundLocationPromptDismissed
                ) {
                    showBackgroundPrompt = true
                }
            }

            if (showBackgroundPrompt) {
                AlertDialog(
                    onDismissRequest = {},
                    title = { Text("Background Location") },
                    text = {
                        Text(
                            "To locate this phone when the screen is off, allow location access \"All the time\". " +
                                "Tap Grant, then choose \"Allow all the time\" in the Settings page that opens."
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = {
                            showBackgroundPrompt = false
                            val intent = android.content.Intent(
                                android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                android.net.Uri.parse("package:${context.packageName}"),
                            )
                            context.startActivity(intent)
                        }) { Text("Grant") }
                    },
                    dismissButton = {
                        TextButton(onClick = {
                            showBackgroundPrompt = false
                            prefs.backgroundLocationPromptDismissed = true
                            showBackgroundDismissed = true
                        }) { Text("Dismiss") }
                    },
                )
            }

            if (showBackgroundDismissed) {
                AlertDialog(
                    onDismissRequest = {},
                    text = { Text("You can grant background location later in Settings.") },
                    confirmButton = {
                        TextButton(onClick = { showBackgroundDismissed = false }) { Text("OK") }
                    },
                )
            }

            // Battery optimization prompt for reliable background downloads
            var showBatteryPrompt by remember { mutableStateOf(false) }
            var showBatteryDismissed by remember { mutableStateOf(false) }

            LaunchedEffect(Unit) {
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.M) {
                    val pm = context.getSystemService(android.os.PowerManager::class.java)
                    if (!pm.isIgnoringBatteryOptimizations(context.packageName)
                        && !prefs.batteryPromptDismissed
                    ) {
                        showBatteryPrompt = true
                    }
                }
            }

            if (showBatteryPrompt) {
                AlertDialog(
                    onDismissRequest = {},
                    title = { Text("Battery Optimization") },
                    text = { Text("For reliable background file downloads, allow Desktop Connector to run without battery restrictions.") },
                    confirmButton = {
                        TextButton(onClick = {
                            showBatteryPrompt = false
                            @Suppress("BatteryLife")
                            val intent = android.content.Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                            intent.data = android.net.Uri.parse("package:${context.packageName}")
                            context.startActivity(intent)
                        }) { Text("Allow") }
                    },
                    dismissButton = {
                        TextButton(onClick = {
                            showBatteryPrompt = false
                            prefs.batteryPromptDismissed = true
                            showBatteryDismissed = true
                        }) { Text("Dismiss") }
                    },
                )
            }

            if (showBatteryDismissed) {
                AlertDialog(
                    onDismissRequest = {},
                    text = { Text("You can change this later in Settings.") },
                    confirmButton = {
                        TextButton(onClick = { showBatteryDismissed = false }) { Text("OK") }
                    },
                )
            }

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

            val authFailureKind by transferViewModel.connectionManager.authFailureKind
                .collectAsState()
            val storageFull by com.desktopconnector.network.StoragePressure.full
                .collectAsState()

            HomeScreen(
                connectionState = connState,
                transfers = transfers,
                isRefreshing = isRefreshing,
                authFailureKind = authFailureKind,
                storageFull = storageFull,
                onFilesSelected = { uris -> transferViewModel.queueFiles(uris) },
                onSendClipboard = { transferViewModel.sendClipboard() },
                onSendUri = { uri -> transferViewModel.queueFiles(listOf(uri)) },
                onItemClick = { transfer -> transferViewModel.onItemClick(transfer) },
                onDelete = { transfer -> transferViewModel.deleteTransfer(transfer) },
                onCancelInFlight = { transfer -> transferViewModel.cancelAndDelete(transfer) },
                isInFlight = { transfer -> transferViewModel.isInFlight(transfer) },
                onRefresh = { transferViewModel.onRefresh() },
                onNavigateSettings = { navController.navigate("settings") },
                onNavigateDownloads = { navController.navigate("downloads") },
                onClearHistory = { transferViewModel.clearHistory() },
                onRepair = {
                    transferViewModel.repairFromAuthFailure()
                    navController.navigate("pairing") {
                        popUpTo("home") { inclusive = false }
                    }
                },
            )
        }

        composable("downloads") {
            FolderScreen(onBack = { navController.popBackStack() })
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
                stage = state.stage,
                errorMessage = state.error,
                onQrScanned = { server, id, pubkey, name ->
                    pairingViewModel.onQrScanned(server, id, pubkey, name)
                },
                verificationCode = state.verificationCode,
                onConfirmPairing = { pairingViewModel.confirmPairing() },
                onRetry = { pairingViewModel.reset() },
                // Cancel exits the pairing screen entirely — previously it
                // reset state to SCANNING which was a no-op since the
                // scanner was already showing.
                onCancel = {
                    pairingViewModel.reset()
                    navController.popBackStack("home", inclusive = false)
                },
            )
        }

        composable("settings") {
            val paired = keyManager.getFirstPairedDevice()
            SettingsScreen(
                prefs = prefs,
                deviceId = keyManager.deviceId,
                pairedDeviceName = paired?.name ?: "",
                pairedDeviceId = paired?.deviceId ?: "",
                themeMode = themeMode,
                onThemeModeChange = onThemeModeChange,
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
                onSendLogs = { text, appendBatteryStats ->
                    // Send logs as a file transfer
                    transferViewModel.sendLogsToDesktop(text, appendBatteryStats)
                },
                onDownloadLogs = { text, appendBatteryStats ->
                    // Download logs to phone storage
                    transferViewModel.downloadLogsToPhone(text, appendBatteryStats)
                },
                onBack = { navController.popBackStack() },
                updateState = updateState,
                onCheckForUpdates = updateViewModel::onForceCheck,
            )
        }
    }
    } // end Column (banner + NavHost)

    // Find my Phone overlay (shown even for silent search)
    if (findPhoneRinging.value) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colorScheme.errorContainer.copy(alpha = 0.95f)),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(16.dp),
                modifier = Modifier.padding(32.dp),
            ) {
                Text(
                    "Find My Phone",
                    style = MaterialTheme.typography.headlineLarge,
                    color = MaterialTheme.colorScheme.onErrorContainer,
                )
                Text(
                    if (FindPhoneManager.isSilent) "Silent search in progress"
                    else "Your desktop is locating this device",
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onErrorContainer,
                    textAlign = TextAlign.Center,
                )

                Spacer(Modifier.height(16.dp))

                Button(
                    onClick = { FindPhoneManager.stopAlarm(findPhoneContext) },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.error,
                        contentColor = MaterialTheme.colorScheme.onError,
                    ),
                    modifier = Modifier
                        .fillMaxWidth(0.6f)
                        .height(56.dp),
                ) {
                    Text("Stop Alarm", style = MaterialTheme.typography.titleMedium)
                }
            }
        }
    }

    UpdateModal(
        state = updateState,
        open = updateModalOpen,
        onInstall = updateViewModel::onInstall,
        onSkip = updateViewModel::onSkipVersion,
        onDismiss = updateViewModel::onDismissModal,
        onCancelDownload = updateViewModel::onCancelDownload,
    )

    } // end Box wrapper
}
