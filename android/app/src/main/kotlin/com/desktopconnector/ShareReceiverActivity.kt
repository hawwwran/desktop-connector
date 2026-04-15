package com.desktopconnector

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.ui.AppNavigation
import com.desktopconnector.ui.theme.DesktopConnectorTheme

/**
 * Handles share intents from other apps.
 * Extracts URIs and passes them to AppNavigation which queues them for upload.
 */
class ShareReceiverActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = AppPreferences(this)
        val keyManager = KeyManager(this)

        val uris = extractUris(intent)

        if (uris.isEmpty()) {
            Toast.makeText(this, "No files to share", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        if (!keyManager.hasPairedDevice()) {
            Toast.makeText(this, "Not paired with a desktop yet. Open the app first.", Toast.LENGTH_LONG).show()
            finish()
            return
        }

        setContent {
            DesktopConnectorTheme {
                AppNavigation(
                    prefs = prefs,
                    keyManager = keyManager,
                    initialUris = uris,
                )
            }
        }

        Toast.makeText(this, "${uris.size} file(s) queued for sending", Toast.LENGTH_SHORT).show()
    }

    private fun extractUris(intent: Intent): List<Uri> {
        val uris = mutableListOf<Uri>()

        when (intent.action) {
            Intent.ACTION_SEND -> {
                intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)?.let { uris.add(it) }
            }
            Intent.ACTION_SEND_MULTIPLE -> {
                intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)?.let { uris.addAll(it) }
            }
        }

        return uris
    }
}
