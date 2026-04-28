package com.desktopconnector.util

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.desktopconnector.data.AppLog
import java.io.File

/**
 * Hands an APK off to the system installer (`ACTION_VIEW` with the
 * `application/vnd.android.package-archive` MIME type), going through
 * the configured `FileProvider` so the dialog can read the bytes.
 *
 * On Android O+ the call is gated on `REQUEST_INSTALL_PACKAGES`. When
 * the permission is missing this helper redirects the user to
 * `MANAGE_UNKNOWN_APP_SOURCES` Settings instead and returns
 * `MISSING_PERMISSION` — the caller surfaces a Toast / banner.
 *
 * Must be called on the main thread (uses `startActivity` and
 * `FileProvider`).
 *
 * Same code path as the original APK-from-history block in
 * `TransferViewModel`; lifted here so the auto-update flow can reuse
 * it without duplication.
 */
object Installer {

    enum class InstallStartOutcome {
        /** System installer dialog opened. App should expect the OS to
         *  replace the package and (optionally) restart this process. */
        LAUNCHED,
        /** APK file no longer exists at the supplied path. */
        FILE_GONE,
        /** REQUEST_INSTALL_PACKAGES not granted on Android O+;
         *  redirected the user to Settings. */
        MISSING_PERMISSION,
        /** Anything else — FileProvider misconfigured, no app to handle
         *  the intent, etc. Detailed cause is in AppLog. */
        ERROR,
    }

    fun installApk(context: Context, apk: File): InstallStartOutcome {
        if (!apk.exists()) {
            AppLog.log("APK", "File missing, path: ${apk.absolutePath}")
            return InstallStartOutcome.FILE_GONE
        }
        AppLog.log("APK", "File exists: true, path: ${apk.absolutePath}")

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
            && !context.packageManager.canRequestPackageInstalls()
        ) {
            AppLog.log("APK", "Unknown sources not allowed")
            try {
                val settingsIntent = Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${context.packageName}")
                ).apply { addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
                context.startActivity(settingsIntent)
            } catch (e: Exception) {
                AppLog.log("APK", "Settings redirect failed: ${e.message}")
            }
            return InstallStartOutcome.MISSING_PERMISSION
        }

        return try {
            val contentUri = FileProvider.getUriForFile(
                context, "${context.packageName}.fileprovider", apk
            )
            AppLog.log("APK", "Installing: $contentUri")
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(contentUri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            InstallStartOutcome.LAUNCHED
        } catch (e: Exception) {
            AppLog.log("APK", "Error: ${e.message}")
            InstallStartOutcome.ERROR
        }
    }
}
