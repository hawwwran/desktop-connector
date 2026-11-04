package com.desktopconnector.service

import android.os.PowerManager
import android.util.Log
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.network.ApiClient
import com.desktopconnector.network.FcmManager
import com.desktopconnector.util.NetworkPolicy
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/**
 * Receives FCM data messages and signals PollService to wake up.
 */
class FcmService : FirebaseMessagingService() {

    override fun onMessageReceived(remoteMessage: RemoteMessage) {
        val type = remoteMessage.data["type"] ?: "unknown"
        AppLog.log("FCM", "fcm.message.received type=$type")
        Log.i("FcmService", "FCM message: $type")

        when (type) {
            "fasttrack" -> {
                PollService.fasttrackWakeSignal = true
                // Cancel any blocking long poll so PollService processes fasttrack immediately
                PollService.activeApi?.cancelLongPoll()
            }
            "ping" -> {
                AppLog.log("FCM", "ping.request.received")
                sendPong()
            }
            // Streaming wakes (D.3). Phase A server fires `stream_ready`
            // when the first chunk is stored (so the recipient starts
            // pulling before the sender finishes) and `abort` when either
            // party DELETEs the transfer mid-stream. Both share the same
            // wake pattern as a classic `transfer_ready`: flip the poll
            // signal AND cancel the long poll so we pick up new chunk
            // availability (or learn about the abort via the next 410)
            // immediately rather than waiting out the 25s block.
            "stream_ready", "abort" -> {
                PollService.fcmWakeSignal = true
                PollService.activeApi?.cancelLongPoll()
            }
            else -> PollService.fcmWakeSignal = true
        }
    }

    /**
     * Run pong synchronously on FCM's own background thread. FCM holds a
     * ~10s wakelock around onMessageReceived, and pongClient caps the request
     * at 3s, so completion is guaranteed before Android can kill the process.
     * Avoids the orphan-Thread pattern where a spawned worker can be reaped
     * mid-request when onMessageReceived returns.
     */
    private fun sendPong() {
        val prefs = AppPreferences(this)
        val server = prefs.serverUrl ?: return
        val id = prefs.deviceId ?: return
        val token = prefs.authToken ?: return
        // Battery-attribution tags: pong over metered cellular while the
        // screen is off is the load-bearing case for radio tail cost (see
        // android_logs_4 analysis, 2026-05-12). 12 pongs/h × ~30 s LTE
        // tail ≈ 60 min/10 h of radio active time billed to the app.
        val pm = getSystemService(PowerManager::class.java)
        val screenOff = pm?.isInteractive == false
        val metered = NetworkPolicy.isMetered(this)
        val tStart = System.currentTimeMillis()
        try {
            val ok = ApiClient(server, id, token).pong()
            val dur = System.currentTimeMillis() - tStart
            AppLog.log("FCM",
                "ping.pong.sent screen_off=$screenOff metered=$metered ok=$ok duration_ms=$dur")
        } catch (e: Exception) {
            val dur = System.currentTimeMillis() - tStart
            AppLog.log("FCM",
                "ping.pong.failed screen_off=$screenOff metered=$metered duration_ms=$dur error_kind=${e.javaClass.simpleName}",
                "warning")
            Log.w("FcmService", "Pong failed: ${e.message}")
        }
    }

    override fun onNewToken(token: String) {
        AppLog.log("FCM", "fcm.token.refreshed")
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
