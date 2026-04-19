package com.desktopconnector

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.ui.AppNavigation
import com.desktopconnector.ui.theme.DesktopConnectorTheme

class MainActivity : ComponentActivity() {

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { results ->
        for ((perm, granted) in results) {
            val short = perm.substringAfterLast('.')
            if (granted) {
                AppLog.log("Platform", "platform.permission.granted permission=$short")
            } else {
                AppLog.log("Platform", "platform.permission.denied permission=$short", "warning")
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        requestPermissions()
        clearTransferNotifications()

        val prefs = AppPreferences(this)
        val keyManager = KeyManager(this)

        setContent {
            DesktopConnectorTheme {
                AppNavigation(
                    prefs = prefs,
                    keyManager = keyManager,
                )
            }
        }
    }

    private fun requestPermissions() {
        val perms = mutableListOf(Manifest.permission.CAMERA)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms.add(Manifest.permission.READ_MEDIA_IMAGES)
            perms.add(Manifest.permission.READ_MEDIA_VIDEO)
            perms.add(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            perms.add(Manifest.permission.READ_EXTERNAL_STORAGE)
        }
        // Location permission is requested separately via the find-phone GPS prompt

        val needed = perms.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (needed.isNotEmpty()) {
            AppLog.log("Platform", "platform.permission.requested permissions=${needed.joinToString(",") { it.substringAfterLast('.') }}")
            permissionLauncher.launch(needed.toTypedArray())
        }

        // Android 11+: request "All files access" for saving to /DesktopConnector
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && !Environment.isExternalStorageManager()) {
            val intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
            intent.data = Uri.parse("package:$packageName")
            startActivity(intent)
        }

        // Battery optimization is requested via dialog in Navigation.kt (like location prompt)

    }

    override fun onResume() {
        super.onResume()
        clearTransferNotifications()
    }

    private fun clearTransferNotifications() {
        val mgr = getSystemService(android.app.NotificationManager::class.java)
        for (notif in mgr.activeNotifications) {
            if (notif.id != 1) {
                mgr.cancel(notif.id)
            }
        }
    }
}
