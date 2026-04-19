package com.desktopconnector.service

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Base64
import android.util.Log
import com.desktopconnector.crypto.CryptoUtils
import com.desktopconnector.data.AppLog
import com.desktopconnector.network.ApiClient
import com.desktopconnector.messaging.DeviceMessage
import kotlinx.coroutines.*
import org.json.JSONObject

/**
 * Manages the find-my-phone alarm: plays loud alarm sound, vibrates,
 * reports GPS coordinates back to desktop via encrypted fasttrack messages.
 */
object FindPhoneManager {

    private const val TAG = "FindPhoneManager"
    private const val NOTIFICATION_ID = 9001
    private const val CHANNEL_ID = "dc_find_phone"
    private const val GPS_INTERVAL_MS = 5000L
    private const val MAX_TIMEOUT_SECONDS = 300 // 5 minutes
    // Brand accent (DcBlue700 = #2058F0) — tints notifications in the shade header.
    private val BRAND_ACCENT = android.graphics.Color.rgb(0x20, 0x58, 0xF0)

    @Volatile var isRinging = false
        private set
    @Volatile var isSilent = false
        private set
    @Volatile var lastLatitude: Double? = null
        private set
    @Volatile var lastLongitude: Double? = null
        private set

    private var mediaPlayer: MediaPlayer? = null
    private var activeVibrator: Vibrator? = null
    private var originalVolume: Int = -1
    private var locationJob: Job? = null
    private var timeoutJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var currentLocation: Location? = null
    private var locationListener: LocationListener? = null

    // Context needed for stop from BroadcastReceiver
    private var activeContext: Context? = null
    private var activeApi: ApiClient? = null
    private var activeDesktopId: String? = null
    private var activeSymmetricKey: ByteArray? = null

    fun handleDeviceMessage(
        context: Context,
        message: DeviceMessage,
        api: ApiClient,
        symmetricKey: ByteArray,
    ) {
        val payload = JSONObject(message.payload)
        val action = payload.optString("action", "")
        val senderId = message.senderId ?: return
        handleCommand(context, action, payload, senderId, api, symmetricKey)
    }

    fun handleCommand(
        context: Context,
        action: String,
        payload: JSONObject,
        senderDeviceId: String,
        api: ApiClient,
        symmetricKey: ByteArray,
    ) {
        when (action) {
            "start" -> {
                var volume = payload.optInt("volume", 80)
                val timeout = MAX_TIMEOUT_SECONDS // always 5 min, ignore client value

                // If silent search not allowed, override with minimum audible alarm
                if (volume == 0) {
                    val prefs = com.desktopconnector.data.AppPreferences(context)
                    if (!prefs.allowSilentSearch) {
                        volume = 30
                        AppLog.log("FindPhone", "Silent search not allowed, overriding volume to 30%")
                    }
                }

                startAlarm(context, volume, timeout, senderDeviceId, api, symmetricKey)
            }
            "stop" -> stopAlarm(context)
            else -> Log.w(TAG, "Unknown find-phone action: $action")
        }
    }

    private fun startAlarm(
        context: Context,
        volume: Int,
        timeout: Int,
        desktopDeviceId: String,
        api: ApiClient,
        symmetricKey: ByteArray,
    ) {
        // Stop any existing alarm first
        if (isRinging) {
            stopAlarmInternal(context, sendUpdate = false)
        }

        activeContext = context
        activeApi = api
        activeDesktopId = desktopDeviceId
        activeSymmetricKey = symmetricKey
        isRinging = true

        val silent = volume == 0
        isSilent = silent
        AppLog.log("FindPhone", "Starting alarm (volume=$volume, timeout=${timeout}s, silent=$silent)")

        // Silent search = GPS tracking only, no sound or vibration
        if (!silent) {
            // Save and set alarm volume
            val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            originalVolume = audioManager.getStreamVolume(AudioManager.STREAM_ALARM)
            val maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_ALARM)
            val targetVolume = (volume / 100.0 * maxVolume).toInt().coerceIn(1, maxVolume)
            audioManager.setStreamVolume(AudioManager.STREAM_ALARM, targetVolume, 0)

            // Play alarm sound
            try {
                val alarmUri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                    ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
                    ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)

                mediaPlayer = MediaPlayer().apply {
                    setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_ALARM)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                            .build()
                    )
                    setDataSource(context, alarmUri)
                    isLooping = true
                    prepare()
                    start()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start alarm sound: ${e.message}")
            }

            // Vibrate — store the instance so we cancel the exact same one
            try {
                val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager
                    vm.defaultVibrator
                } else {
                    @Suppress("DEPRECATION")
                    context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
                }
                activeVibrator = vibrator
                val pattern = longArrayOf(0, 1000, 500)
                vibrator.vibrate(VibrationEffect.createWaveform(pattern, 0))
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start vibration: ${e.message}")
            }
        }

        // Show notification with stop button (skip for silent search)
        if (!silent) {
            showAlarmNotification(context)
        }

        // Start active location tracking (getLastKnownLocation returns stale/null data)
        startLocationUpdates(context)

        // Send initial "ringing" update before starting GPS loop to avoid race
        scope.launch {
            sendUpdate(context, desktopDeviceId, api, symmetricKey, "ringing")
        }

        // Start GPS reporting (after initial update)
        locationJob = scope.launch {
            delay(GPS_INTERVAL_MS) // first GPS update after interval since initial was just sent
            reportLocationLoop(context, desktopDeviceId, api, symmetricKey)
        }

        // Auto-stop after timeout
        timeoutJob = scope.launch {
            delay(timeout * 1000L)
            if (isRinging) {
                AppLog.log("FindPhone", "Timeout reached, stopping alarm")
                stopAlarm(context)
            }
        }
    }

    fun stopAlarm(context: Context) {
        stopAlarmInternal(context, sendUpdate = true)
    }

    private fun stopAlarmInternal(context: Context, sendUpdate: Boolean) {
        if (!isRinging && mediaPlayer == null) return

        AppLog.log("FindPhone", "Stopping alarm")
        isRinging = false

        // Stop media player
        try {
            mediaPlayer?.stop()
            mediaPlayer?.release()
        } catch (_: Exception) {}
        mediaPlayer = null

        // Stop vibrator — cancel the same instance that started it
        try {
            activeVibrator?.cancel()
        } catch (_: Exception) {}
        activeVibrator = null

        // Restore volume
        if (originalVolume >= 0) {
            try {
                val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                audioManager.setStreamVolume(AudioManager.STREAM_ALARM, originalVolume, 0)
            } catch (_: Exception) {}
            originalVolume = -1
        }

        // Stop location updates
        stopLocationUpdates(context)

        // Cancel coroutines
        locationJob?.cancel()
        timeoutJob?.cancel()
        locationJob = null
        timeoutJob = null

        // Dismiss notification
        val mgr = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        mgr.cancel(NOTIFICATION_ID)

        // Send "stopped" update
        if (sendUpdate) {
            val api = activeApi
            val desktopId = activeDesktopId
            val key = activeSymmetricKey
            if (api != null && desktopId != null && key != null) {
                scope.launch {
                    sendUpdate(context, desktopId, api, key, "stopped")
                }
            }
        }

        activeContext = null
        activeApi = null
        activeDesktopId = null
        activeSymmetricKey = null
        lastLatitude = null
        lastLongitude = null
        isSilent = false
    }

    private suspend fun reportLocationLoop(
        context: Context,
        desktopDeviceId: String,
        api: ApiClient,
        symmetricKey: ByteArray,
    ) {
        while (isRinging) {
            try {
                sendUpdate(context, desktopDeviceId, api, symmetricKey, "ringing")
            } catch (e: Exception) {
                AppLog.log("FindPhone", "Location update failed: ${e.message}")
            }
            delay(GPS_INTERVAL_MS)
        }
    }

    private fun startLocationUpdates(context: Context) {
        val locationManager = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
        val gpsEnabled = locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)
        val netEnabled = locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
        AppLog.log("FindPhone", "Location providers: GPS=$gpsEnabled, Network=$netEnabled")

        if (!gpsEnabled && !netEnabled) {
            AppLog.log("FindPhone", "No location providers enabled — GPS will be unavailable")
            return
        }

        val listener = object : LocationListener {
            override fun onLocationChanged(location: Location) {
                // Never log raw lat/lng — GPS coords stay off-disk.
                AppLog.log("FindPhone", "Location update acc=${location.accuracy} provider=${location.provider}")
                currentLocation = location
                lastLatitude = location.latitude
                lastLongitude = location.longitude
            }
            override fun onLocationChanged(locations: MutableList<Location>) {
                if (locations.isNotEmpty()) onLocationChanged(locations.last())
            }
            @Deprecated("Deprecated") override fun onStatusChanged(provider: String?, status: Int, extras: android.os.Bundle?) {}
            override fun onProviderEnabled(provider: String) {
                AppLog.log("FindPhone", "Provider enabled: $provider")
            }
            override fun onProviderDisabled(provider: String) {
                AppLog.log("FindPhone", "Provider disabled: $provider")
            }
        }
        locationListener = listener

        // Register on main thread — some OEMs require this
        android.os.Handler(android.os.Looper.getMainLooper()).post {
            try {
                @Suppress("MissingPermission")
                if (gpsEnabled) {
                    locationManager.requestLocationUpdates(LocationManager.GPS_PROVIDER, 2000L, 0f, listener)
                    AppLog.log("FindPhone", "Registered GPS listener")
                }
                @Suppress("MissingPermission")
                if (netEnabled) {
                    locationManager.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 2000L, 0f, listener)
                    AppLog.log("FindPhone", "Registered Network listener")
                }
            } catch (e: SecurityException) {
                AppLog.log("FindPhone", "No location permission: ${e.message}")
            } catch (e: Exception) {
                AppLog.log("FindPhone", "Failed to register listener: ${e.message}")
            }
        }

        // Seed with last known as fallback
        try {
            @Suppress("MissingPermission")
            val cached = locationManager.getLastKnownLocation(LocationManager.GPS_PROVIDER)
                ?: locationManager.getLastKnownLocation(LocationManager.NETWORK_PROVIDER)
            if (cached != null) {
                currentLocation = cached
                lastLatitude = cached.latitude
                lastLongitude = cached.longitude
                // Never log raw lat/lng — GPS coords stay off-disk.
                AppLog.log("FindPhone", "Cached location acc=${cached.accuracy} age=${(System.currentTimeMillis() - cached.time) / 1000}s")
            } else {
                AppLog.log("FindPhone", "No cached location available")
            }
        } catch (e: SecurityException) {
            AppLog.log("FindPhone", "No permission for cached location")
        }

        AppLog.log("FindPhone", "Location updates started")
    }

    private fun stopLocationUpdates(context: Context) {
        val listener = locationListener ?: return
        try {
            val locationManager = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            locationManager.removeUpdates(listener)
            AppLog.log("FindPhone", "Location updates stopped")
        } catch (_: Exception) {}
        locationListener = null
        currentLocation = null
    }

    private fun sendUpdate(
        context: Context,
        desktopDeviceId: String,
        api: ApiClient,
        symmetricKey: ByteArray,
        state: String,
    ) {
        val payload = JSONObject().apply {
            put("fn", "find-phone")
            put("state", state)
        }

        val location = currentLocation
        if (location != null) {
            payload.put("lat", location.latitude)
            payload.put("lng", location.longitude)
            payload.put("accuracy", location.accuracy.toDouble())
            lastLatitude = location.latitude
            lastLongitude = location.longitude
        }
        AppLog.log("FindPhone", "sendUpdate state=$state hasLocation=${location != null}")

        val plainBytes = payload.toString().toByteArray()
        val encrypted = CryptoUtils.encryptBlob(plainBytes, symmetricKey)
        val encryptedB64 = Base64.encodeToString(encrypted, Base64.NO_WRAP)
        api.fasttrackSend(desktopDeviceId, encryptedB64)
    }

    private fun showAlarmNotification(context: Context) {
        val stopIntent = Intent(context, StopReceiver::class.java)
        val stopPending = PendingIntent.getBroadcast(
            context, 0, stopIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val notification = Notification.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
            .setColor(BRAND_ACCENT)
            .setContentTitle("Find My Phone")
            .setContentText("Your phone is being located")
            .setOngoing(true)
            .setCategory(Notification.CATEGORY_ALARM)
            .addAction(
                Notification.Action.Builder(
                    null, "Stop Alarm", stopPending
                ).build()
            )
            .build()

        val mgr = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        mgr.notify(NOTIFICATION_ID, notification)
    }

    /**
     * BroadcastReceiver to stop the alarm from the notification action button.
     */
    class StopReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            AppLog.log("FindPhone", "Stop from notification")
            stopAlarm(context)
        }
    }
}
