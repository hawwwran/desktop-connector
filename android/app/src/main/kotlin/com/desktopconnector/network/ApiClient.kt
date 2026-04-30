package com.desktopconnector.network

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit


/** Why the server rejected our credentials. 401 vs 403 mark different
 *  recovery scopes — see ConnectionManager / HomeScreen consumers. */
enum class AuthFailureKind { CREDENTIALS_INVALID, PAIRING_MISSING }


/** Every authenticated HTTP call funnels a verdict through this flow so
 *  ConnectionManager can maintain the consecutive-failure counter.
 *  `peerId` attributes 403 PAIRING_MISSING failures to a specific paired
 *  desktop; null = global / not attributable. 401 CREDENTIALS_INVALID
 *  failures are always global (the phone's own creds are bad regardless
 *  of who we were calling). */
sealed class AuthObservation {
    object Success : AuthObservation()
    data class Failure(val kind: AuthFailureKind, val peerId: String? = null) : AuthObservation()
}

data class DeviceRegistrationResult(
    val statusCode: Int,
    val body: JSONObject?,
) {
    val isSuccessful: Boolean
        get() = statusCode in 200..299 && body != null
}

/**
 * HTTP client for all server API endpoints.
 * Mirrors the Python desktop/src/api_client.py implementation.
 */
class ApiClient(
    private val serverUrl: String,
    private var deviceId: String = "",
    private var authToken: String = "",
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    // Separate client for long poll with longer read timeout
    private val longPollClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(35, TimeUnit.SECONDS)
        .build()

    private val jsonType = "application/json; charset=utf-8".toMediaType()
    private val binaryType = "application/octet-stream".toMediaType()

    companion object {
        // Global auth-observation bus: every ApiClient (view-model's and
        // PollService's short-lived ones) emits to the same flow so one
        // ConnectionManager can aggregate the full picture. extraBufferCapacity
        // makes tryEmit non-suspending under burst polling.
        private val _authObservations = MutableSharedFlow<AuthObservation>(
            extraBufferCapacity = 16,
        )
        val authObservations: SharedFlow<AuthObservation> = _authObservations.asSharedFlow()

        internal fun emitAuth(observation: AuthObservation) {
            _authObservations.tryEmit(observation)
        }
    }

    fun setCredentials(deviceId: String, authToken: String) {
        this.deviceId = deviceId
        this.authToken = authToken
    }

    private fun reportAuthStatus(code: Int, peerId: String? = null) {
        val o = when (code) {
            in 200..299 -> AuthObservation.Success
            // 401 is account-level (the phone's creds are bad), so peer
            // attribution is meaningless. 403 is per-pair, so we
            // attribute when the caller knew who they were addressing.
            401 -> AuthObservation.Failure(AuthFailureKind.CREDENTIALS_INVALID)
            403 -> AuthObservation.Failure(AuthFailureKind.PAIRING_MISSING, peerId)
            else -> return  // 4xx/5xx for other reasons are not auth verdicts
        }
        emitAuth(o)
    }

    private fun authHeaders(builder: Request.Builder): Request.Builder {
        return builder
            .header("X-Device-ID", deviceId)
            .header("Authorization", "Bearer $authToken")
    }

    // --- Device Registration ---

    fun register(publicKeyB64: String, deviceType: String = "phone"): JSONObject? {
        val result = registerWithStatus(publicKeyB64, deviceType)
        return if (result?.isSuccessful == true) result.body else null
    }

    fun registerWithStatus(publicKeyB64: String, deviceType: String = "phone"): DeviceRegistrationResult? {
        val body = JSONObject().apply {
            put("public_key", publicKeyB64)
            put("device_type", deviceType)
        }
        val request = Request.Builder()
            .url("$serverUrl/api/devices/register")
            .post(body.toString().toRequestBody(jsonType))
            .build()
        return executeRegistration(request)
    }

    // --- Pairing ---

    fun sendPairingRequest(desktopId: String, phonePubkey: String): Boolean {
        val body = JSONObject().apply {
            put("desktop_id", desktopId)
            put("phone_pubkey", phonePubkey)
        }
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/pairing/request")
            .post(body.toString().toRequestBody(jsonType))
            .build()
        return executeStatus(request, peerId = desktopId)
    }

    // --- Transfers ---

    enum class InitOutcome { OK, STORAGE_FULL, TOO_LARGE, FAILED }

    /**
     * Init result for the streaming-aware call path. `outcome` carries
     * the same coarse verdict as the legacy `initTransfer` returns;
     * `negotiatedMode ∈ {"classic", "streaming"}` is what the server
     * accepted (may be downgraded from what the sender requested).
     * D.4a will make `UploadWorker` read this value to pick its loop.
     */
    data class InitResult(val outcome: InitOutcome, val negotiatedMode: String)

    /**
     * Typed outcome for a streaming chunk upload. Callers distinguish:
     *   - Ok              — chunk stored.
     *   - StorageFull     — 507: recipient quota full mid-stream;
     *                       sender backs off and retries (WAITING_STREAM).
     *   - Aborted(reason) — 410: counterparty aborted; sender stops.
     *   - AuthError       — 401/403; ConnectionManager latches via
     *                       the auth-observation flow (already emitted
     *                       by the time this returns).
     *   - NetworkError    — transport/read/connect error; transient.
     *   - ServerError(n)  — any other 4xx/5xx; treat as transient.
     *
     * Legacy `uploadChunk` returning JSONObject? stays in place for
     * the classic path until D.4a migrates it. Same shape for
     * ChunkDownloadResult.
     */
    sealed class ChunkUploadResult {
        object Ok : ChunkUploadResult()
        object StorageFull : ChunkUploadResult()
        data class Aborted(val reason: String?) : ChunkUploadResult()
        object AuthError : ChunkUploadResult()
        object NetworkError : ChunkUploadResult()
        data class ServerError(val code: Int) : ChunkUploadResult()
    }

    /**
     * Typed outcome for a streaming chunk download.
     *   - Ok(bytes)          — raw encrypted chunk bytes.
     *   - TooEarly(retryMs)  — 425: upstream hasn't produced this chunk
     *                          yet. `retryMs` comes from the server's
     *                          `retry_after_ms` JSON body (ms precision);
     *                          falls back to `Retry-After` header * 1000
     *                          and finally to 1000 ms.
     *   - Aborted(reason)    — 410: counterparty aborted.
     *   - AuthError / NetworkError / ServerError — as above.
     */
    sealed class ChunkDownloadResult {
        data class Ok(val bytes: ByteArray) : ChunkDownloadResult()
        data class TooEarly(val retryAfterMs: Long) : ChunkDownloadResult()
        data class Aborted(val reason: String?) : ChunkDownloadResult()
        object AuthError : ChunkDownloadResult()
        object NetworkError : ChunkDownloadResult()
        data class ServerError(val code: Int) : ChunkDownloadResult()
    }

    fun initTransfer(transferId: String, recipientId: String, encryptedMeta: String, chunkCount: Int): InitOutcome {
        return initTransferTyped(transferId, recipientId, encryptedMeta, chunkCount, "classic").outcome
    }

    /**
     * Streaming-aware init. `mode = "streaming"` asks the server to run
     * the transfer as an overlapping upload/delivery pipeline;
     * `mode = "classic"` (default) keeps the store-then-forward path.
     * The server may downgrade `streaming` → `classic` if the recipient
     * isn't fresh enough, if streaming is disabled via config, or if
     * the client didn't advertise the stream_v1 capability.
     *
     * Clients that want streaming should call `getCapabilities()` first
     * and only pass `mode = "streaming"` when `"stream_v1"` is present.
     * Guard against streaming for `.fn.*` transfers at the caller.
     */
    fun initTransferTyped(
        transferId: String,
        recipientId: String,
        encryptedMeta: String,
        chunkCount: Int,
        mode: String = "classic",
    ): InitResult {
        val body = JSONObject().apply {
            put("transfer_id", transferId)
            put("recipient_id", recipientId)
            put("encrypted_meta", encryptedMeta)
            put("chunk_count", chunkCount)
            put("mode", mode)
        }
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/init")
            .post(body.toString().toRequestBody(jsonType))
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code, peerId = recipientId)
                val bodyStr = resp.body?.string()
                val negotiated = parseNegotiatedMode(bodyStr) ?: "classic"
                val outcome = when (resp.code) {
                    201 -> InitOutcome.OK
                    // 507 = recipient's pending-bytes quota exhausted.
                    // Transient; caller treats as WAITING and retries.
                    507 -> InitOutcome.STORAGE_FULL
                    // 413 = this specific transfer exceeds the server's
                    // cap. Terminal — no retry, no WAITING.
                    413 -> InitOutcome.TOO_LARGE
                    else -> InitOutcome.FAILED
                }
                InitResult(outcome, negotiated)
            }
        } catch (e: Exception) {
            InitResult(InitOutcome.FAILED, "classic")
        }
    }

    private fun parseNegotiatedMode(bodyStr: String?): String? {
        if (bodyStr.isNullOrBlank()) return null
        return try {
            JSONObject(bodyStr).optString("negotiated_mode").takeIf { it.isNotBlank() }
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Either-party abort. `reason ∈ {sender_abort, sender_failed,
     * recipient_abort}` — the server validates that the reason lines
     * up with the caller's role and returns 400 on cross-role mistakes.
     * Server accepts a body-less DELETE too (back-compat with old
     * clients), but new code should always pass a typed reason.
     *
     * Returns true on 2xx (abort succeeded OR transfer was already
     * delivered — the server reports this truthfully and doesn't flip
     * a delivered row to aborted). Returns false on non-2xx or
     * transport error; callers that care about the specific status
     * can parse the JSON body out-of-band, but for Phase D's sender
     * state machine the bool verdict is enough.
     */
    fun abortTransfer(transferId: String, reason: String, peerId: String? = null): Boolean {
        val body = JSONObject().apply { put("reason", reason) }
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId")
            .delete(body.toString().toRequestBody(jsonType))
            .build()
        return executeStatus(request, peerId = peerId)
    }

    fun uploadChunk(transferId: String, chunkIndex: Int, data: ByteArray, peerId: String? = null): JSONObject? {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/chunks/$chunkIndex")
            .post(data.toRequestBody(binaryType))
            .build()
        return executeJson(request, peerId = peerId)
    }

    /**
     * Streaming-aware chunk upload. Distinguishes the 507 mid-stream
     * backpressure case (sender flips to WAITING_STREAM, keeps row
     * alive, retries later) from 410 Gone (counterparty aborted,
     * terminal) and from plain network flakes. The legacy
     * `uploadChunk` above returns JSONObject? and collapses all of
     * these into null; D.4a's streaming sender uses this typed path
     * instead.
     */
    fun uploadChunkTyped(transferId: String, chunkIndex: Int, data: ByteArray, peerId: String? = null): ChunkUploadResult {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/chunks/$chunkIndex")
            .post(data.toRequestBody(binaryType))
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code, peerId = peerId)
                when {
                    resp.isSuccessful -> ChunkUploadResult.Ok
                    resp.code == 401 || resp.code == 403 -> ChunkUploadResult.AuthError
                    resp.code == 507 -> ChunkUploadResult.StorageFull
                    resp.code == 410 -> ChunkUploadResult.Aborted(parseAbortReason(resp.body?.string()))
                    else -> ChunkUploadResult.ServerError(resp.code)
                }
            }
        } catch (e: Exception) {
            ChunkUploadResult.NetworkError
        }
    }

    /** Parse `abort_reason` out of a 410 Gone JSON body, or null. */
    private fun parseAbortReason(bodyStr: String?): String? {
        if (bodyStr.isNullOrBlank()) return null
        return try {
            JSONObject(bodyStr).optString("abort_reason").takeIf { it.isNotBlank() }
        } catch (_: Exception) {
            null
        }
    }

    fun getPendingTransfers(): List<JSONObject> {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/pending")
            .get()
            .build()
        val result = executeJson(request) ?: return emptyList()
        val arr = result.optJSONArray("transfers") ?: return emptyList()
        return (0 until arr.length()).map { arr.getJSONObject(it) }
    }

    fun getStats(pairedWith: String? = null): JSONObject? {
        val url = if (pairedWith != null) "$serverUrl/api/devices/stats?paired_with=$pairedWith"
                  else "$serverUrl/api/devices/stats"
        val request = authHeaders(Request.Builder())
            .url(url)
            .get()
            .build()
        return executeJson(request, peerId = pairedWith)
    }

    // Active long poll call — can be cancelled externally to unblock the poll loop
    @Volatile private var activeLongPollCall: okhttp3.Call? = null

    fun cancelLongPoll() {
        activeLongPollCall?.cancel()
        activeLongPollCall = null
    }

    fun longPollNotify(sinceEpoch: Long, test: Boolean = false): JSONObject? {
        val url = if (test) {
            "$serverUrl/api/transfers/notify?test=1"
        } else {
            "$serverUrl/api/transfers/notify?since=$sinceEpoch"
        }
        val request = authHeaders(Request.Builder())
            .url(url)
            .get()
            .build()
        // Use short timeout for test, long timeout for actual long poll
        val httpClient = if (test) client else longPollClient
        val call = httpClient.newCall(request)
        if (!test) activeLongPollCall = call
        return try {
            call.execute().use { resp ->
                activeLongPollCall = null
                reportAuthStatus(resp.code)
                val body = resp.body?.string() ?: return null
                if (resp.isSuccessful) JSONObject(body) else null
            }
        } catch (e: Exception) {
            activeLongPollCall = null
            null
        }
    }

    fun getSentStatus(): List<JSONObject> {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/sent-status")
            .get()
            .build()
        val result = executeJson(request) ?: return emptyList()
        val arr = result.optJSONArray("transfers") ?: return emptyList()
        return (0 until arr.length()).map { arr.getJSONObject(it) }
    }

    fun downloadChunk(transferId: String, chunkIndex: Int, peerId: String? = null): ByteArray? {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/chunks/$chunkIndex")
            .get()
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code, peerId = peerId)
                if (resp.isSuccessful) resp.body?.bytes() else null
            }
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Streaming-aware chunk download. Distinguishes 425 (upstream
     * hasn't produced this chunk yet — poll again after the server's
     * `retry_after_ms` hint) from 410 (counterparty aborted) from
     * plain network errors. The legacy `downloadChunk` above
     * collapses all of these into null; D.3's streaming recipient
     * uses this typed path instead.
     */
    fun downloadChunkTyped(transferId: String, chunkIndex: Int, peerId: String? = null): ChunkDownloadResult {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/chunks/$chunkIndex")
            .get()
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code, peerId = peerId)
                when {
                    resp.isSuccessful -> {
                        val bytes = resp.body?.bytes()
                        if (bytes != null) ChunkDownloadResult.Ok(bytes)
                        else ChunkDownloadResult.NetworkError
                    }
                    resp.code == 401 || resp.code == 403 -> ChunkDownloadResult.AuthError
                    resp.code == 425 -> {
                        val retryMs = parseRetryAfterMs(resp)
                        ChunkDownloadResult.TooEarly(retryMs)
                    }
                    resp.code == 410 -> ChunkDownloadResult.Aborted(parseAbortReason(resp.body?.string()))
                    else -> ChunkDownloadResult.ServerError(resp.code)
                }
            }
        } catch (e: Exception) {
            ChunkDownloadResult.NetworkError
        }
    }

    /**
     * Resolve a retry hint from a 425 response. Prefer the ms-precision
     * `retry_after_ms` field in the JSON body (the server emits this
     * explicitly); fall back to the standard `Retry-After` header
     * (seconds → milliseconds); final fallback is 1 s.
     */
    private fun parseRetryAfterMs(resp: okhttp3.Response): Long {
        val bodyStr = resp.body?.string()
        val bodyMs = if (!bodyStr.isNullOrBlank()) {
            try {
                JSONObject(bodyStr).optLong("retry_after_ms", -1L)
            } catch (_: Exception) {
                -1L
            }
        } else -1L
        if (bodyMs > 0) return bodyMs
        val headerSec = resp.header("Retry-After")?.toLongOrNull()
        if (headerSec != null && headerSec > 0) return headerSec * 1000L
        return 1000L
    }

    fun ackTransfer(transferId: String, peerId: String? = null): Boolean {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/ack")
            .post("".toRequestBody(jsonType))
            .build()
        return executeStatus(request, peerId = peerId)
    }

    /**
     * Per-chunk ACK for streaming transfers. Signals that the
     * recipient has safely received and decrypted chunk `chunkIndex`;
     * the server deletes that chunk's blob immediately (streaming's
     * peak on-disk = ~1 chunk instead of N). The final chunk's ACK
     * also flips the transfer to delivered — recipients should NOT
     * send a transfer-level `ackTransfer` after per-chunk ACKing the
     * last chunk.
     */
    fun ackChunk(transferId: String, chunkIndex: Int, peerId: String? = null): Boolean {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/chunks/$chunkIndex/ack")
            .post("".toRequestBody(jsonType))
            .build()
        return executeStatus(request, peerId = peerId)
    }

    // Short-timeout client for liveness pong — must fit inside FCM's
    // ~10s onMessageReceived wakelock even on slow networks.
    private val pongClient = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(3, TimeUnit.SECONDS)
        .build()

    fun pong(): Boolean {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/devices/pong")
            .post("".toRequestBody(jsonType))
            .build()
        return try {
            pongClient.newCall(request).execute().use {
                reportAuthStatus(it.code)
                it.isSuccessful
            }
        } catch (e: Exception) {
            false
        }
    }

    fun pingDevice(recipientId: String): JSONObject? {
        val body = JSONObject().apply { put("recipient_id", recipientId) }
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/devices/ping")
            .post(body.toString().toRequestBody(jsonType))
            .build()
        return executeJson(request)
    }

    fun updateFcmToken(token: String): Boolean {
        val body = JSONObject().apply {
            put("fcm_token", token)
        }
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/devices/fcm-token")
            .post(body.toString().toRequestBody(jsonType))
            .build()
        return executeStatus(request)
    }

    // --- Fasttrack: lightweight encrypted message relay ---

    fun fasttrackSend(recipientId: String, encryptedData: String): Int? {
        val body = JSONObject().apply {
            put("recipient_id", recipientId)
            put("encrypted_data", encryptedData)
        }
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/fasttrack/send")
            .post(body.toString().toRequestBody(jsonType))
            .build()
        val result = executeJson(request, peerId = recipientId) ?: return null
        return result.optInt("message_id", -1).takeIf { it > 0 }
    }

    fun fasttrackPending(): List<JSONObject> {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/fasttrack/pending")
            .get()
            .build()
        val result = executeJson(request) ?: return emptyList()
        val arr = result.optJSONArray("messages") ?: return emptyList()
        return (0 until arr.length()).map { arr.getJSONObject(it) }
    }

    fun fasttrackAck(messageId: Int): Boolean {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/fasttrack/$messageId/ack")
            .post("".toRequestBody(jsonType))
            .build()
        return executeStatus(request)
    }


    // Fast client for heartbeats — 1s timeout so it never blocks the poll loop
    private val heartbeatClient = OkHttpClient.Builder()
        .connectTimeout(1, TimeUnit.SECONDS)
        .readTimeout(1, TimeUnit.SECONDS)
        .build()

    // --- Capability probe ---
    //
    // /api/health is optional-auth; we probe it without credentials so
    // a client in a latched 401/403 state can still pick up streaming
    // availability once pairing recovers. 60 s TTL is short enough to
    // notice a server-side streamingEnabled flip reasonably quickly
    // and long enough to avoid hammering the endpoint on every
    // transfer init. Values are memoised per-ApiClient-instance;
    // PollService / UploadWorker use their own instances so the cache
    // does not outlive a process lifetime.
    @Volatile private var capabilitiesCache: Set<String>? = null
    @Volatile private var capabilitiesCacheExpiryMs: Long = 0L
    private val capabilitiesTtlMs = 60_000L

    fun getCapabilities(): Set<String> {
        val now = System.currentTimeMillis()
        val cached = capabilitiesCache
        if (cached != null && now < capabilitiesCacheExpiryMs) {
            return cached
        }
        val request = Request.Builder()
            .url("$serverUrl/api/health")
            .get()
            .build()
        val fresh: Set<String> = try {
            client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) return@use emptySet<String>()
                val body = resp.body?.string() ?: return@use emptySet<String>()
                val json = JSONObject(body)
                val arr = json.optJSONArray("capabilities") ?: return@use emptySet<String>()
                (0 until arr.length()).mapNotNull { idx ->
                    arr.optString(idx).takeIf { it.isNotBlank() }
                }.toSet()
            }
        } catch (e: Exception) {
            // On a transient failure, fall through with an empty set
            // (classic fallback is correct) and still cache briefly so
            // extended outages don't turn into a probe loop.
            emptySet()
        }
        capabilitiesCache = fresh
        capabilitiesCacheExpiryMs = now + capabilitiesTtlMs
        return fresh
    }

    fun healthCheck(fast: Boolean = false): Boolean {
        // Use an authenticated endpoint so a 200 genuinely proves "we can
        // reach the server AND our creds are valid". /api/health was
        // optional-auth and 200'd even with stale creds — that's how the
        // persistent notification and tray could show "online" while every
        // authed call was getting 401'd.
        return try {
            val request = authHeaders(Request.Builder())
                .url("$serverUrl/api/transfers/pending")
                .get()
                .build()
            val c = if (fast) heartbeatClient else client
            c.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code)
                resp.isSuccessful
            }
        } catch (e: Exception) {
            false
        }
    }

    private fun executeJson(request: Request, peerId: String? = null): JSONObject? {
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code, peerId)
                val body = resp.body?.string() ?: return null
                if (resp.isSuccessful) JSONObject(body) else null
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun executeRegistration(request: Request): DeviceRegistrationResult? {
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code)
                val rawBody = resp.body?.string().orEmpty()
                val jsonBody = try {
                    if (rawBody.isBlank()) null else JSONObject(rawBody)
                } catch (_: Exception) {
                    null
                }
                DeviceRegistrationResult(
                    statusCode = resp.code,
                    body = jsonBody,
                )
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun executeStatus(request: Request, peerId: String? = null): Boolean {
        return try {
            client.newCall(request).execute().use {
                reportAuthStatus(it.code, peerId)
                it.isSuccessful
            }
        } catch (e: Exception) {
            false
        }
    }
}
