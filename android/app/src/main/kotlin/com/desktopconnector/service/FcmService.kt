package com.desktopconnector.service

import android.util.Log
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.network.FcmManager
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/**
 * Receives FCM data messages and signals PollService to wake up.
 */
class FcmService : FirebaseMessagingService() {

    override fun onMessageReceived(remoteMessage: RemoteMessage) {
        val type = remoteMessage.data["type"] ?: "unknown"
        AppLog.log("FCM", "Message received: $type")
        Log.i("FcmService", "FCM message: $type")

        when (type) {
            "fasttrack" -> {
                PollService.fasttrackWakeSignal = true
                // Cancel any blocking long poll so PollService processes fasttrack immediately
                PollService.activeApi?.cancelLongPoll()
            }
            else -> PollService.fcmWakeSignal = true
        }
    }

    override fun onNewToken(token: String) {
        AppLog.log("FCM", "Token refreshed")
        Log.i("FcmService", "New FCM token")
        val prefs = AppPreferences(this)
        if (prefs.isRegistered && prefs.serverUrl != null) {
            Thread {
                try {
                    FcmManager.registerToken(prefs)
                } catch (e: Exception) {
                    Log.w("FcmService", "Token re-registration failed: ${e.message}")
                }
            }.start()
        }
    }
}
