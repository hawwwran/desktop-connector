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
 * Supports both file URIs (EXTRA_STREAM) and text/URLs (EXTRA_TEXT).
 */
class ShareReceiverActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = AppPreferences(this)
        val keyManager = KeyManager(this)

        if (!keyManager.hasPairedDevice()) {
            Toast.makeText(this, "Not paired with a desktop yet. Open the app first.", Toast.LENGTH_LONG).show()
            finish()
            return
        }

        val uris = extractUris(intent)
        val sharedText = extractText(intent)

        if (uris.isEmpty() && sharedText == null) {
            Toast.makeText(this, "Nothing to share", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        setContent {
            DesktopConnectorTheme {
                AppNavigation(
                    prefs = prefs,
                    keyManager = keyManager,
                    initialUris = uris,
                    initialClipboardText = sharedText,
                )
            }
        }

        if (uris.isNotEmpty()) {
            Toast.makeText(this, "${uris.size} file(s) queued for sending", Toast.LENGTH_SHORT).show()
        } else if (sharedText != null) {
            val preview = if (sharedText.length > 30) sharedText.take(30) + "..." else sharedText
            Toast.makeText(this, "Sending: $preview", Toast.LENGTH_SHORT).show()
        }
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

    private fun extractText(intent: Intent): String? {
        if (intent.action != Intent.ACTION_SEND) return null
        // Only use EXTRA_TEXT if no file URI was shared
        if (intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM) != null) return null
        return intent.getStringExtra(Intent.EXTRA_TEXT)?.takeIf { it.isNotBlank() }
    }
}
