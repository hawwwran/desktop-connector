package com.desktopconnector.data

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Simple file-based log, capped at MAX_LINES. Viewable in Settings.
 */
object AppLog {
    private const val MAX_LINES = 2000
    private const val FILENAME = "app.log"
    private lateinit var logFile: File
    private val dateFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    fun init(context: Context) {
        logFile = File(context.filesDir, FILENAME)
    }

    fun log(tag: String, message: String) {
        if (!::logFile.isInitialized) return
        val ts = dateFormat.format(Date())
        val line = "$ts [$tag] $message\n"
        try {
            logFile.appendText(line)
            trimIfNeeded()
        } catch (_: Exception) {}
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
