package com.desktopconnector.data

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Simple file-based log, capped at MAX_LINES. Viewable in Settings.
 *
 * Privacy rule (must never be violated):
 *   Never log confidential fields — symmetric or private keys, auth_token,
 *   FCM token, decrypted clipboard text/images, decrypted file bytes,
 *   fasttrack payloads, or GPS coordinates. Log only non-sensitive
 *   metadata: transfer_id / message_id / device_id (first 12 chars),
 *   sizes, outcomes, and error_kind.
 */
object AppLog {
    private const val MAX_LINES = 2000
    private const val FILENAME = "app.log"
    private lateinit var logFile: File
    private var enabled = false
    private val dateFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    fun init(context: Context) {
        logFile = File(context.filesDir, FILENAME)
        enabled = AppPreferences(context).loggingEnabled
    }

    fun log(tag: String, message: String, level: String = "info") {
        if (!::logFile.isInitialized || !enabled) return
        val ts = dateFormat.format(Date())
        val line = "$ts [$level] [$tag] $message\n"
        try {
            logFile.appendText(line)
            trimIfNeeded()
        } catch (_: Exception) {}
    }

    /** Call after preference change to pick up new state. */
    fun refreshEnabled(context: Context) {
        enabled = AppPreferences(context).loggingEnabled
    }

    fun read(): String {
        if (!::logFile.isInitialized || !logFile.exists()) return ""
        return try { logFile.readText() } catch (_: Exception) { "" }
    }

    fun clear() {
        if (::logFile.isInitialized) {
            try { logFile.writeText("") } catch (_: Exception) {}
        }
    }

    private fun trimIfNeeded() {
        try {
            val lines = logFile.readLines()
            if (lines.size > MAX_LINES) {
                logFile.writeText(lines.takeLast(MAX_LINES).joinToString("\n") + "\n")
            }
        } catch (_: Exception) {}
    }
}
