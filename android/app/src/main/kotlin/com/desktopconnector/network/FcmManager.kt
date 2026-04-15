package com.desktopconnector.network

import android.content.Context
import android.util.Log
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import com.google.firebase.messaging.FirebaseMessaging
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Manages dynamic Firebase initialization from server-provided config
 * and FCM token registration. Safe to call multiple times.
 */
object FcmManager {
    private const val TAG = "FcmManager"

    @Volatile
    var isInitialized = false
        private set

    @Volatile
    var initAttempted = false
        private set

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .build()

    /**
     * Attempt FCM initialization from server config. Returns true if Firebase
     * is ready (or was already initialized). Returns false on any failure.
     */
    /**
     * Reset init state so the next initialize() call tries again.
     * Call after pairing or server URL change.
     */
    fun reset() {
        initAttempted = false
    }

    fun initialize(context: Context, prefs: AppPreferences): Boolean {
        if (isInitialized) return true
        initAttempted = true

        val serverUrl = prefs.serverUrl ?: return false

        val config = fetchFcmConfig(serverUrl)
        if (config == null || !config.optBoolean("available", false)) {
            return false
        }

        val projectId = config.optString("project_id", "")
        val appId = config.optString("application_id", "")
        val apiKey = config.optString("api_key", "")
        val senderId = config.optString("gcm_sender_id", "")

        if (projectId.isEmpty() || appId.isEmpty() || apiKey.isEmpty() || senderId.isEmpty()) {
            AppLog.log("FCM", "Server config incomplete")
            return false
        }

        try {
            val options = FirebaseOptions.Builder()
                .setProjectId(projectId)
                .setApplicationId(appId)
                .setApiKey(apiKey)
                .setGcmSenderId(senderId)
                .build()

            if (FirebaseApp.getApps(context).isEmpty()) {
                FirebaseApp.initializeApp(context, options)
            }
            isInitialized = true
            AppLog.log("FCM", "Firebase initialized")
        } catch (e: Exception) {
            if (FirebaseApp.getApps(context).isNotEmpty()) {
                isInitialized = true
            } else {
                Log.w(TAG, "Firebase init failed: ${e.message}")
                AppLog.log("FCM", "Init failed: ${e.message}")
                return false
            }
        }

        registerToken(prefs)
        return true
    }

    /**
     * Get current FCM token and send to server if it changed.
     */
    fun registerToken(prefs: AppPreferences) {
        try {
            FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
                val storedToken = prefs.fcmToken
                if (token != storedToken) {
                    val serverUrl = prefs.serverUrl ?: return@addOnSuccessListener
                    val deviceId = prefs.deviceId ?: return@addOnSuccessListener
                    val authToken = prefs.authToken ?: return@addOnSuccessListener

                    Thread {
                        try {
                            val api = ApiClient(serverUrl, deviceId, authToken)
                            if (api.updateFcmToken(token)) {
                                prefs.fcmToken = token
                                AppLog.log("FCM", "Token registered")
                            }
                        } catch (e: Exception) {
                            Log.w(TAG, "Token registration failed: ${e.message}")
                        }
                    }.start()
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to get FCM token: ${e.message}")
        }
    }

    private fun fetchFcmConfig(serverUrl: String): JSONObject? {
        return try {
            val request = Request.Builder()
                .url("$serverUrl/api/fcm/config")
                .get()
                .build()
            httpClient.newCall(request).execute().use { resp ->
                if (resp.isSuccessful) {
                    val body = resp.body?.string() ?: return null
                    JSONObject(body)
                } else null
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to fetch FCM config: ${e.message}")
            null
        }
    }
}
