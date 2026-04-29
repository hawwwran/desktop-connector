package com.desktopconnector.data

import android.content.Context
import android.content.SharedPreferences
import com.desktopconnector.crypto.SelectedPairPref

/**
 * Non-sensitive app preferences (server URL, registration state).
 * Sensitive keys are in EncryptedSharedPreferences via KeyManager.
 */
class AppPreferences(context: Context) : SelectedPairPref {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("dc_prefs", Context.MODE_PRIVATE)

    var serverUrl: String?
        get() = prefs.getString("server_url", null)
        set(value) = prefs.edit().putString("server_url", value).apply()

    var deviceId: String?
        get() = prefs.getString("device_id", null)
        set(value) = prefs.edit().putString("device_id", value).apply()

    var authToken: String?
        get() = prefs.getString("auth_token", null)
        set(value) = prefs.edit().putString("auth_token", value).apply()

    var fcmToken: String?
        get() = prefs.getString("fcm_token", null)
        set(value) = prefs.edit().putString("fcm_token", value).apply()

    /** Currently selected paired desktop. Null = use most-recently-paired
     *  fallback (resolved by `PairingRepository.selected`). */
    override var selectedDeviceId: String?
        get() = prefs.getString("selected_device_id", null)
        set(value) = prefs.edit().run {
            if (value == null) remove("selected_device_id") else putString("selected_device_id", value)
            apply()
        }

    /** One-shot gate for `MultiPairMigrationRunner`. */
    var multiPairMigrationDone: Boolean
        get() = prefs.getBoolean("multi_pair_migration_done", false)
        set(value) = prefs.edit().putBoolean("multi_pair_migration_done", value).apply()

    val isRegistered: Boolean
        get() = deviceId != null && authToken != null

    var locationPromptDismissed: Boolean
        get() = prefs.getBoolean("location_prompt_dismissed", false)
        set(value) = prefs.edit().putBoolean("location_prompt_dismissed", value).apply()

    var backgroundLocationPromptDismissed: Boolean
        get() = prefs.getBoolean("background_location_prompt_dismissed", false)
        set(value) = prefs.edit().putBoolean("background_location_prompt_dismissed", value).apply()

    var loggingEnabled: Boolean
        get() = prefs.getBoolean("logging_enabled", false)
        set(value) = prefs.edit().putBoolean("logging_enabled", value).apply()

    var allowSilentSearch: Boolean
        get() = prefs.getBoolean("allow_silent_search", true)
        set(value) = prefs.edit().putBoolean("allow_silent_search", value).apply()

    var batteryPromptDismissed: Boolean
        get() = prefs.getBoolean("battery_prompt_dismissed", false)
        set(value) = prefs.edit().putBoolean("battery_prompt_dismissed", value).apply()

    var lastUpdateCheckAt: Long
        get() = prefs.getLong("last_update_check_at", 0L)
        set(value) = prefs.edit().putLong("last_update_check_at", value).apply()

    // Defensive copy on get — SharedPreferences' getStringSet warns against
    // mutating the returned instance.
    var dismissedUpdateVersions: Set<String>
        get() = prefs.getStringSet("dismissed_update_versions", null)?.toSet() ?: emptySet()
        set(value) = prefs.edit().putStringSet("dismissed_update_versions", value).apply()

    var cachedLatestVersion: String?
        get() = prefs.getString("cached_latest_version", null)
        set(value) = prefs.edit().putString("cached_latest_version", value).apply()

    fun dismissUpdateVersion(version: String) {
        dismissedUpdateVersions = dismissedUpdateVersions + version
    }

    fun isUpdateVersionDismissed(version: String): Boolean =
        version in dismissedUpdateVersions

    var themeMode: ThemeMode
        get() = when (prefs.getString("theme_mode", "system")) {
            "light" -> ThemeMode.LIGHT
            "dark" -> ThemeMode.DARK
            else -> ThemeMode.SYSTEM
        }
        set(value) = prefs.edit().putString(
            "theme_mode",
            when (value) {
                ThemeMode.LIGHT -> "light"
                ThemeMode.DARK -> "dark"
                ThemeMode.SYSTEM -> "system"
            }
        ).apply()

    fun clear() {
        prefs.edit().clear().apply()
    }

    /** Drop the device_id/auth_token pair so the next app start re-registers
     *  with the server. Leaves theme/server URL/other prefs alone. */
    fun clearAuthCredentials() {
        prefs.edit()
            .remove("device_id")
            .remove("auth_token")
            .apply()
    }
}
