package com.desktopconnector.network

import com.desktopconnector.data.AppLog
import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
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

/** Consecutive auth failures required before the banner surfaces. See the
 *  matching constant in desktop's connection.py for why 3 works across both
 *  polling cadences. */
private const val AUTH_FAILURE_THRESHOLD = 3

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

    // Orthogonal to `state`: a device with bad creds can still reach
    // optional-auth endpoints (/api/health) and flip to CONNECTED. The
    // banner watches this flag independently and overrides other copy.
    private val _authFailureKind = MutableStateFlow<AuthFailureKind?>(null)
    val authFailureKind: StateFlow<AuthFailureKind?> = _authFailureKind.asStateFlow()

    /** Tracks a "streak pending" flag independent of the latched kind so the
     *  UI can show offline as soon as the first 401/403 lands, not only
     *  after the banner trips. */
    private val _authStreaking = MutableStateFlow(false)

    /** State folded for UI consumption. Online = network OK AND no auth
     *  failure outstanding (neither latched nor in a pending streak). Keeps
     *  the UI from claiming "online" while a 401/403 is unresolved. */
    @OptIn(kotlinx.coroutines.DelicateCoroutinesApi::class)
    val effectiveState: StateFlow<ConnectionState> = combine(
        _state, _authFailureKind, _authStreaking,
    ) { s, k, streaking ->
        if (k != null || streaking) ConnectionState.DISCONNECTED else s
    }.stateIn(GlobalScope, SharingStarted.Eagerly, ConnectionState.DISCONNECTED)

    // Streak state — guarded by `authStreakLock` so the collector
    // thread (observeAuth) can't interleave with the UI thread
    // (clearAuthFailure) on the non-atomic counter updates.
    private val authStreakLock = Any()
    private var authFailureStreak = 0
    private var authFailureStreakKind: AuthFailureKind? = null

    /** Feed an AuthObservation emitted by ApiClient.authObservations. Safe
     *  to call from any thread. */
    fun observeAuth(observation: AuthObservation) = synchronized(authStreakLock) {
        when (observation) {
            is AuthObservation.Success -> {
                authFailureStreak = 0
                authFailureStreakKind = null
                _authStreaking.value = false
                // Do NOT clear _authFailureKind here — the flag stays latched
                // until the user acknowledges via the banner (see
                // clearAuthFailure()). Otherwise an optional-auth 200 would
                // silently dismiss a real failure.
            }
            is AuthObservation.Failure -> {
                // Already latched? Don't re-fire, don't advance counter.
                if (_authFailureKind.value != null) return@synchronized
                if (authFailureStreakKind != observation.kind) {
                    authFailureStreakKind = observation.kind
                    authFailureStreak = 1
                } else {
                    authFailureStreak++
                }
                // Flip effective state to DISCONNECTED on the very first
                // failure. Banner still waits for the 3-count threshold.
                _authStreaking.value = true
                if (authFailureStreak >= AUTH_FAILURE_THRESHOLD) {
                    _authFailureKind.value = observation.kind
                    AppLog.log("Auth",
                        "auth.failure.tripped kind=${observation.kind.name} count=$authFailureStreak",
                        "warning")
                }
            }
        }
    }

    fun clearAuthFailure() = synchronized(authStreakLock) {
        authFailureStreak = 0
        authFailureStreakKind = null
        _authStreaking.value = false
        _authFailureKind.value = null
    }

    val secondsUntilRetry: Int
        get() {
            val info = _retryInfo.value
            return maxOf(0, ((info.nextRetryAt - System.currentTimeMillis()) / 1000).toInt())
        }

    fun checkConnection(): Boolean {
        // Use an authenticated endpoint so 200 means "network AND creds
        // both work". /api/health was optional-auth so it 200'd even with
        // bad creds — that's why the app used to claim "online" before any
        // authed call actually succeeded.
        _state.value = ConnectionState.RECONNECTING
        return try {
            val builder = Request.Builder()
                .url("${serverUrl}/api/transfers/pending")
                .get()
            if (deviceId.isNotEmpty() && authToken.isNotEmpty()) {
                builder.header("X-Device-ID", deviceId)
                builder.header("Authorization", "Bearer $authToken")
            }
            val response = client.newCall(builder.build()).execute()
            response.use {
                // Feed the verdict into the shared auth-observation bus so
                // the banner counter stays in sync whether the ping comes
                // from here or from ApiClient.
                when (it.code) {
                    in 200..299 -> ApiClient.emitAuth(AuthObservation.Success)
                    401 -> ApiClient.emitAuth(
                        AuthObservation.Failure(AuthFailureKind.CREDENTIALS_INVALID))
                    403 -> ApiClient.emitAuth(
                        AuthObservation.Failure(AuthFailureKind.PAIRING_MISSING))
                }
                if (it.isSuccessful) {
                    onSuccess()
                    true
                } else if (it.code == 401 || it.code == 403) {
                    // Auth rejection — don't advance backoff (retries won't
                    // fix stale creds); effectiveState already flipped via
                    // the bus path.
                    false
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
        if (_authFailureKind.value != null) {
            return "Offline — pairing lost on server"
        }
        // Pending streak also reads as offline so the text matches the dot.
        if (_authStreaking.value) {
            return "Offline — verifying credentials"
        }
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
