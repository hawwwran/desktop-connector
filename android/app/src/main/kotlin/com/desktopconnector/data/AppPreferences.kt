package com.desktopconnector.data

import android.content.Context
import android.content.SharedPreferences

/**
 * Non-sensitive app preferences (server URL, registration state).
 * Sensitive keys are in EncryptedSharedPreferences via KeyManager.
 */
class AppPreferences(context: Context) {

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

    val isRegistered: Boolean
        get() = deviceId != null && authToken != null

    fun clear() {
        prefs.edit().clear().apply()
    }
}
