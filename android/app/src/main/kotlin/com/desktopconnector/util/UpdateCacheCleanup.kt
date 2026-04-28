package com.desktopconnector.util

import android.content.Context
import com.desktopconnector.data.AppLog
import java.io.File

/**
 * Prunes APKs cached by [com.desktopconnector.network.UpdateDownloader]
 * older than [DEFAULT_MAX_AGE_DAYS]. Called from `MainActivity.onCreate`
 * once per process so a successful install (or a download the user
 * walked away from) doesn't leave 36 MB sitting on disk indefinitely.
 */
object UpdateCacheCleanup {

    private const val DEFAULT_MAX_AGE_DAYS = 7

    /** Production entry — uses the app's cache dir. */
    fun pruneOldUpdates(context: Context, maxAgeDays: Int = DEFAULT_MAX_AGE_DAYS) {
        pruneOldUpdates(File(context.cacheDir, "updates"), maxAgeDays)
    }

    /** Test-friendly entry — operates on the supplied directory. */
    internal fun pruneOldUpdates(updatesDir: File, maxAgeDays: Int = DEFAULT_MAX_AGE_DAYS) {
        if (!updatesDir.isDirectory) return
        val cutoff = System.currentTimeMillis() - maxAgeDays.toLong() * 24L * 60 * 60 * 1000
        val files = updatesDir.listFiles() ?: return
        var deleted = 0
        for (f in files) {
            if (f.isFile && f.lastModified() < cutoff && f.delete()) deleted++
        }
        if (deleted > 0) {
            AppLog.log("UpdateDownload", "Pruned $deleted old cached APK(s)")
        }
    }
}
