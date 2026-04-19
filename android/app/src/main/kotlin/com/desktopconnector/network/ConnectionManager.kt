package com.desktopconnector.network

import com.desktopconnector.data.AppLog
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.random.Random

enum class ConnectionState { CONNECTED, DISCONNECTED, RECONNECTING }

data class RetryInfo(
    val retryCount: Int = 0,
    val currentBackoff: Double = INITIAL_BACKOFF,
    val nextRetryAt: Long = 0,
)

private const val INITIAL_BACKOFF = 2.0
private const val BACKOFF_MULTIPLIER = 2.0
private const val MAX_BACKOFF = 300.0
private const val JITTER = 0.2

class ConnectionManager(
    var serverUrl: String,
    var deviceId: String = "",
    var authToken: String = "",
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(3, TimeUnit.SECONDS)
        .build()

    private val _state = MutableStateFlow(ConnectionState.DISCONNECTED)
    val state: StateFlow<ConnectionState> = _state.asStateFlow()

    private val _retryInfo = MutableStateFlow(RetryInfo())
    val retryInfo: StateFlow<RetryInfo> = _retryInfo.asStateFlow()

    val secondsUntilRetry: Int
        get() {
            val info = _retryInfo.value
            return maxOf(0, ((info.nextRetryAt - System.currentTimeMillis()) / 1000).toInt())
        }

    fun checkConnection(): Boolean {
        _state.value = ConnectionState.RECONNECTING
        return try {
            val builder = Request.Builder()
                .url("${serverUrl}/api/health")
                .get()
            // Send auth headers so server updates last_seen (heartbeat)
            if (deviceId.isNotEmpty() && authToken.isNotEmpty()) {
                builder.header("X-Device-ID", deviceId)
                builder.header("Authorization", "Bearer $authToken")
            }
            val response = client.newCall(builder.build()).execute()
            response.use {
                if (it.isSuccessful) {
                    onSuccess()
                    true
                } else {
                    onFailure("http_${it.code}")
                    false
                }
            }
        } catch (e: Exception) {
            onFailure(e.javaClass.simpleName)
            false
        }
    }

    fun onSuccess() {
        val wasReconnecting = _state.value != ConnectionState.CONNECTED
        _retryInfo.value = RetryInfo()
        _state.value = ConnectionState.CONNECTED
        if (wasReconnecting) {
            AppLog.log("Connection", "connection.check.succeeded")
        }
    }

    fun onFailure(errorKind: String = "unknown") {
        val info = _retryInfo.value
        val newCount = info.retryCount + 1
        val raw = min(INITIAL_BACKOFF * Math.pow(BACKOFF_MULTIPLIER, (newCount - 1).toDouble()), MAX_BACKOFF)
        val jitterRange = raw * JITTER
        val backoff = raw + Random.nextDouble(-jitterRange, jitterRange)
        _retryInfo.value = RetryInfo(
            retryCount = newCount,
            currentBackoff = backoff,
            nextRetryAt = System.currentTimeMillis() + (backoff * 1000).toLong(),
        )
        _state.value = ConnectionState.DISCONNECTED
        val level = if (newCount > 3) "warning" else "info"
        AppLog.log("Connection",
            "connection.backoff.retry attempt=$newCount delay_seconds=%.1f error_kind=$errorKind".format(backoff),
            level,
        )
    }

    fun tryNow() {
        _retryInfo.value = RetryInfo()
        checkConnection()
    }

    fun getStatusText(): String {
        return when (_state.value) {
            ConnectionState.CONNECTED -> "Connected"
            ConnectionState.RECONNECTING -> "Connecting..."
            ConnectionState.DISCONNECTED -> {
                val info = _retryInfo.value
                "Offline — retry #${info.retryCount} in ${secondsUntilRetry}s"
            }
        }
    }
}
