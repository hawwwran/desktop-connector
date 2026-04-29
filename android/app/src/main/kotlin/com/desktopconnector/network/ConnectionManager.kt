package com.desktopconnector.network

import com.desktopconnector.data.AppLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
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

    // Singleton-lifetime scope for derived flows. ConnectionManager itself
    // is owned by TransferViewModel which lives for the process; never
    // cancelled.
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private val _state = MutableStateFlow(ConnectionState.DISCONNECTED)
    val state: StateFlow<ConnectionState> = _state.asStateFlow()

    private val _retryInfo = MutableStateFlow(RetryInfo())
    val retryInfo: StateFlow<RetryInfo> = _retryInfo.asStateFlow()

    /** Latched 401/403 verdicts keyed by peer id. The `""` (empty-string)
     *  key is reserved for global failures — every CREDENTIALS_INVALID
     *  lands here (the phone's creds are bad regardless of who we were
     *  calling), and 403s without a peer attribution land here too.
     *  Per-peer keys hold one of those device ids. */
    private val _authFailureByPeer = MutableStateFlow<Map<String, AuthFailureKind>>(emptyMap())
    val authFailureByPeer: StateFlow<Map<String, AuthFailureKind>> = _authFailureByPeer.asStateFlow()

    /** Aggregate "any failure latched" view. Kept for the connection-state
     *  fold and any consumer that just needs a yes/no. */
    val anyAuthFailure: StateFlow<Boolean> = _authFailureByPeer
        .map { it.isNotEmpty() }
        .stateIn(scope, SharingStarted.Eagerly, false)

    /** Tracks a "streak pending" flag independent of the latched map so the
     *  UI can show offline as soon as the first 401/403 lands, not only
     *  after the banner trips. */
    private val _authStreaking = MutableStateFlow(false)

    /** State folded for UI consumption. Online = network OK AND no auth
     *  failure outstanding (neither latched nor in a pending streak). Keeps
     *  the UI from claiming "online" while a 401/403 is unresolved. */
    val effectiveState: StateFlow<ConnectionState> = combine(
        _state, anyAuthFailure, _authStreaking,
    ) { s, anyFailure, streaking ->
        if (anyFailure || streaking) ConnectionState.DISCONNECTED else s
    }.stateIn(scope, SharingStarted.Eagerly, ConnectionState.DISCONNECTED)

    // Per-key streak counters. CREDENTIALS_INVALID and PAIRING_MISSING
    // keep separate counts: a 401 then 403 to different peers must NOT
    // collapse into a 2-streak. Guarded by `authStreakLock` so the
    // collector thread (observeAuth) can't interleave with the UI thread
    // (clearAuthFailure) on the non-atomic counter updates.
    private val authStreakLock = Any()
    private val streakByKey = HashMap<Pair<String, AuthFailureKind>, Int>()

    /** Feed an AuthObservation emitted by ApiClient.authObservations. Safe
     *  to call from any thread. */
    fun observeAuth(observation: AuthObservation) = synchronized(authStreakLock) {
        when (observation) {
            is AuthObservation.Success -> {
                streakByKey.clear()
                _authStreaking.value = false
                // Do NOT clear latched failures here — they stay latched
                // until the user acknowledges via the banner (see
                // clearAuthFailure(peerId)). Otherwise an optional-auth
                // 200 would silently dismiss a real failure.
            }
            is AuthObservation.Failure -> {
                // CREDENTIALS_INVALID is always app-global — the empty
                // string is the canonical "global" key. PAIRING_MISSING
                // attributes to the peer if known; falls back to global.
                val key = if (observation.kind == AuthFailureKind.CREDENTIALS_INVALID) ""
                          else observation.peerId ?: ""
                val mapKey = key to observation.kind
                // Already latched for this key? Don't re-fire, don't bump.
                if (_authFailureByPeer.value[key] != null) return@synchronized
                val next = (streakByKey[mapKey] ?: 0) + 1
                streakByKey[mapKey] = next
                _authStreaking.value = true
                if (next >= AUTH_FAILURE_THRESHOLD) {
                    _authFailureByPeer.value = _authFailureByPeer.value + (key to observation.kind)
                    streakByKey.remove(mapKey)
                    AppLog.log("Auth",
                        "auth.failure.tripped kind=${observation.kind.name} peer=${key.take(12)} count=$next",
                        "warning")
                }
            }
        }
    }

    fun clearAuthFailure(peerId: String) = synchronized(authStreakLock) {
        _authFailureByPeer.value = _authFailureByPeer.value - peerId
        // Drop streak counters for this key too — leaving them would let
        // a single subsequent 401/403 immediately re-latch.
        streakByKey.entries.removeAll { it.key.first == peerId }
        if (_authFailureByPeer.value.isEmpty() && streakByKey.isEmpty()) {
            _authStreaking.value = false
        }
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
}
