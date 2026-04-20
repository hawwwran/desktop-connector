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
     * Reset init state AND the cached token so the next initialize() call
     * fully re-runs *and* the next registerToken() actually POSTs.
     *
     * FCM tokens survive pair cycles (and even app reinstalls — the token
     * is keyed on installation + sender_id). Without also clearing
     * prefs.fcmToken, the `if (token != storedToken)` check inside
     * registerToken() skips the POST, and a fresh server record (e.g.
     * after a re-pair or server-side DB wipe) never learns this device's
     * FCM token — every subsequent push comes back `fcm_result=no_token`
     * and the phone can't be woken.
     */
    fun reset(prefs: AppPreferences? = null) {
        isInitialized = false
        initAttempted = false
        prefs?.fcmToken = null
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
     * Get current FCM token and send to server.
     *
     * Always POSTs — the previous "only if token changed vs prefs cache"
     * optimization caused a silent failure mode: FCM tokens survive
     * pair cycles and even reinstalls (keyed on installation + sender),
     * so whenever the server's record was wiped (re-pair, DB restore),
     * the phone thought it had already registered and skipped the POST,
     * leaving the server with no_token on every wake and the tray stuck
     * painting "phone offline".
     *
     * One extra POST per init is cheap and keeps the server record
     * authoritatively in sync. We still cache the token in prefs for
     * diagnostics / audit but don't gate on it.
     */
    fun registerToken(prefs: AppPreferences) {
        try {
            FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
                val serverUrl = prefs.serverUrl ?: return@addOnSuccessListener
                val deviceId = prefs.deviceId ?: return@addOnSuccessListener
                val authToken = prefs.authToken ?: return@addOnSuccessListener

                Thread {
                    try {
                        val api = ApiClient(serverUrl, deviceId, authToken)
                        if (api.updateFcmToken(token)) {
                            prefs.fcmToken = token
                            AppLog.log("FCM", "Token registered")
                        } else {
                            AppLog.log("FCM", "Token registration failed (non-2xx)", "warning")
                        }
                    } catch (e: Exception) {
                        Log.w(TAG, "Token registration failed: ${e.message}")
                        AppLog.log("FCM", "Token registration exception: ${e.javaClass.simpleName}", "warning")
                    }
                }.start()
            }.addOnFailureListener { e ->
                Log.w(TAG, "Failed to get FCM token: ${e.message}")
                AppLog.log("FCM", "Failed to get token: ${e.javaClass.simpleName}", "warning")
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
