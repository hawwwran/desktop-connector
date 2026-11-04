package com.desktopconnector.util

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import com.desktopconnector.network.FcmManager

/**
 * Network-aware policy gate for our two polling loops (TransferViewModel
 * heartbeat + PollService long-poll). On metered cellular each long-poll
 * cycle costs several seconds of modem standby tail; multiplied across a
 * day this is the dominant battery cost (see android_logs_3 analysis,
 * 2026-05-11: 95 mAh of 99 mAh app drain was mobile_radio active time).
 *
 * When FCM is healthy + the network is metered, we'd rather wait for an
 * FCM wake than keep the modem warm — FCM is HIGH priority and bypasses
 * Doze, so a transfer-ready signal still arrives within a second or two.
 */
object NetworkPolicy {

    /** True when the active network is metered (typically cellular or a
     *  hotspot). Wi-Fi reports unmetered unless the user explicitly
     *  marked it metered. Returns false on any error (permissions, no
     *  network, etc.) so the safe default is "treat as cheap and poll
     *  normally". */
    fun isMetered(context: Context): Boolean {
        return try {
            val cm = context.getSystemService(ConnectivityManager::class.java) ?: return false
            val net = cm.activeNetwork ?: return false
            val caps = cm.getNetworkCapabilities(net) ?: return false
            !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
        } catch (_: Exception) {
            false
        }
    }

    /** Headline gate: skip the active polling branches when we're on a
     *  metered network AND FCM is initialized (so we'll still hear about
     *  transfers via push). Caller is responsible for waking on the
     *  network-unmetered transition, FCM wake, or screen-state change. */
    fun shouldSkipPollingForBattery(context: Context): Boolean {
        return FcmManager.isInitialized && isMetered(context)
    }
}
