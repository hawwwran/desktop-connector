package com.desktopconnector.ui.pairing

import android.Manifest
import android.util.Log
import android.util.Size
import androidx.camera.core.*
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.desktopconnector.ui.theme.brandColors
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import org.json.JSONObject

@Composable
fun PairingScreen(
    onQrScanned: (serverUrl: String, desktopId: String, desktopPubkey: String, desktopName: String) -> Unit,
    verificationCode: String?,
    onConfirmPairing: () -> Unit,
    onCancel: () -> Unit,
) {
    var scannedData by remember { mutableStateOf<JSONObject?>(null) }
    var manualMode by remember { mutableStateOf(false) }
    var manualServerUrl by remember { mutableStateOf("http://") }
    val hasPermission = rememberCameraPermission()

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            "Pair with Desktop",
            style = MaterialTheme.typography.headlineMedium,
            color = MaterialTheme.colorScheme.onBackground,
        )
        Spacer(Modifier.height(8.dp))

        if (verificationCode != null) {
            // Verification stage
            Text(
                "Verify this code matches your desktop:",
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(24.dp))
            Text(
                verificationCode,
                fontSize = 36.sp,
                fontFamily = FontFamily.Monospace,
                color = MaterialTheme.brandColors.accentYellow,
            )
            Spacer(Modifier.height(32.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                OutlinedButton(onClick = onCancel) {
                    Text("Cancel")
                }
                Button(onClick = onConfirmPairing) {
                    Text("Codes Match")
                }
            }
        } else if (!hasPermission && !manualMode) {
            Spacer(Modifier.height(48.dp))
            Text(
                "Camera permission is needed to scan the QR code from your desktop.",
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(24.dp))
            TextButton(onClick = { manualMode = true }) {
                Text("Enter server URL manually")
            }
        } else if (manualMode) {
            // Manual server URL entry
            Text(
                "Enter the server URL shown on your desktop",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(16.dp))
            OutlinedTextField(
                value = manualServerUrl,
                onValueChange = { manualServerUrl = it },
                label = { Text("Server URL") },
                placeholder = { Text("http://192.168.1.x:4441") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                "After entering the URL, scan the QR code or the desktop will show its Device ID and public key in the terminal.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(16.dp))

            if (hasPermission) {
                Text(
                    "Scan QR code:",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(8.dp))
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .aspectRatio(1f)
                        .weight(1f, fill = false),
                ) {
                    QrScanner(
                        onScanned = { raw ->
                            try {
                                val json = JSONObject(raw)
                                // Use manual URL if provided, otherwise from QR
                                val server = if (manualServerUrl.length > 10) manualServerUrl.trim() else json.getString("server")
                                val deviceId = json.getString("device_id")
                                val pubkey = json.getString("pubkey")
                                val name = json.optString("name", "Desktop")
                                scannedData = json
                                onQrScanned(server, deviceId, pubkey, name)
                            } catch (e: Exception) {
                                Log.w("PairingScreen", "Invalid QR data: $raw")
                            }
                        }
                    )
                }
            }

            Spacer(Modifier.height(16.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                OutlinedButton(onClick = { manualMode = false }) {
                    Text("Back")
                }
            }
        } else {
            // Scanner stage
            Text(
                "Scan the QR code shown on your desktop",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(16.dp))
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f)
                    .clip(RoundedCornerShape(16.dp)),
            ) {
                QrScanner(
                    onScanned = { raw ->
                        try {
                            val json = JSONObject(raw)
                            val server = json.getString("server")
                            val deviceId = json.getString("device_id")
                            val pubkey = json.getString("pubkey")
                            val name = json.optString("name", "Desktop")
                            scannedData = json
                            onQrScanned(server, deviceId, pubkey, name)
                        } catch (e: Exception) {
                            Log.w("PairingScreen", "Invalid QR data: $raw")
                        }
                    }
                )
            }
            Spacer(Modifier.height(16.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                OutlinedButton(onClick = onCancel) {
                    Text("Cancel")
                }
                TextButton(onClick = { manualMode = true }) {
                    Text("Enter URL manually")
                }
            }
        }
    }
}

@Composable
private fun QrScanner(onScanned: (String) -> Unit) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    var scannedOnce by remember { mutableStateOf(false) }

    AndroidView(
        factory = { ctx ->
            val previewView = PreviewView(ctx)
            val cameraProviderFuture = ProcessCameraProvider.getInstance(ctx)

            cameraProviderFuture.addListener({
                val cameraProvider = cameraProviderFuture.get()
                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }

                val analyzer = ImageAnalysis.Builder()
                    .setTargetResolution(Size(1280, 720))
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()

                val scanner = BarcodeScanning.getClient()

                analyzer.setAnalyzer(ContextCompat.getMainExecutor(ctx)) { imageProxy ->
                    @androidx.camera.core.ExperimentalGetImage
                    val mediaImage = imageProxy.image
                    if (mediaImage != null && !scannedOnce) {
                        val image = InputImage.fromMediaImage(mediaImage, imageProxy.imageInfo.rotationDegrees)
                        scanner.process(image)
                            .addOnSuccessListener { barcodes ->
                                for (barcode in barcodes) {
                                    if (barcode.valueType == Barcode.TYPE_TEXT && !scannedOnce) {
                                        scannedOnce = true
                                        barcode.rawValue?.let { onScanned(it) }
                                    }
                                }
                            }
                            .addOnCompleteListener {
                                imageProxy.close()
                            }
                    } else {
                        imageProxy.close()
                    }
                }

                try {
                    cameraProvider.unbindAll()
                    cameraProvider.bindToLifecycle(
                        lifecycleOwner,
                        CameraSelector.DEFAULT_BACK_CAMERA,
                        preview,
                        analyzer,
                    )
                } catch (e: Exception) {
                    Log.e("QrScanner", "Camera bind failed", e)
                }
            }, ContextCompat.getMainExecutor(ctx))

            previewView
        },
        modifier = Modifier.fillMaxSize(),
    )
}

@Composable
private fun rememberCameraPermission(): Boolean {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    var granted by remember { mutableStateOf(false) }

    // Re-check on every resume (permission may have been granted while away)
    DisposableEffect(lifecycleOwner) {
        val observer = androidx.lifecycle.LifecycleEventObserver { _, event ->
            if (event == androidx.lifecycle.Lifecycle.Event.ON_RESUME) {
                granted = ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                        android.content.pm.PackageManager.PERMISSION_GRANTED
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    return granted
}
