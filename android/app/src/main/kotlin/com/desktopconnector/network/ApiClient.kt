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
 *  ConnectionManager can maintain the consecutive-failure counter. */
sealed class AuthObservation {
    object Success : AuthObservation()
    data class Failure(val kind: AuthFailureKind) : AuthObservation()
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

    private fun reportAuthStatus(code: Int) {
        val o = when (code) {
            in 200..299 -> AuthObservation.Success
            401 -> AuthObservation.Failure(AuthFailureKind.CREDENTIALS_INVALID)
            403 -> AuthObservation.Failure(AuthFailureKind.PAIRING_MISSING)
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
        val body = JSONObject().apply {
            put("public_key", publicKeyB64)
            put("device_type", deviceType)
        }
        val request = Request.Builder()
            .url("$serverUrl/api/devices/register")
            .post(body.toString().toRequestBody(jsonType))
            .build()
        return executeJson(request)
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
        return executeStatus(request)
    }

    // --- Transfers ---

    enum class InitOutcome { OK, STORAGE_FULL, TOO_LARGE, FAILED }

    fun initTransfer(transferId: String, recipientId: String, encryptedMeta: String, chunkCount: Int): InitOutcome {
        val body = JSONObject().apply {
            put("transfer_id", transferId)
            put("recipient_id", recipientId)
            put("encrypted_meta", encryptedMeta)
            put("chunk_count", chunkCount)
        }
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/init")
            .post(body.toString().toRequestBody(jsonType))
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code)
                when (resp.code) {
                    201 -> InitOutcome.OK
                    // 507 = recipient's pending-bytes quota exhausted.
                    // Transient; caller treats as WAITING and retries.
                    507 -> InitOutcome.STORAGE_FULL
                    // 413 = this specific transfer exceeds the server's
                    // cap. Terminal — no retry, no WAITING.
                    413 -> InitOutcome.TOO_LARGE
                    else -> InitOutcome.FAILED
                }
            }
        } catch (e: Exception) {
            InitOutcome.FAILED
        }
    }

    /** Sender-initiated cancel. Server deletes chunks + rows; a
     *  still-downloading recipient gets 404 on next chunk fetch and
     *  abandons gracefully. */
    fun cancelTransfer(transferId: String): Boolean {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId")
            .delete()
            .build()
        return executeStatus(request)
    }

    fun uploadChunk(transferId: String, chunkIndex: Int, data: ByteArray): JSONObject? {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/chunks/$chunkIndex")
            .post(data.toRequestBody(binaryType))
            .build()
        return executeJson(request)
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
        return executeJson(request)
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

    fun downloadChunk(transferId: String, chunkIndex: Int): ByteArray? {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/chunks/$chunkIndex")
            .get()
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code)
                if (resp.isSuccessful) resp.body?.bytes() else null
            }
        } catch (e: Exception) {
            null
        }
    }

    fun ackTransfer(transferId: String): Boolean {
        val request = authHeaders(Request.Builder())
            .url("$serverUrl/api/transfers/$transferId/ack")
            .post("".toRequestBody(jsonType))
            .build()
        return executeStatus(request)
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
        val result = executeJson(request) ?: return null
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

    private fun executeJson(request: Request): JSONObject? {
        return try {
            client.newCall(request).execute().use { resp ->
                reportAuthStatus(resp.code)
                val body = resp.body?.string() ?: return null
                if (resp.isSuccessful) JSONObject(body) else null
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun executeStatus(request: Request): Boolean {
        return try {
            client.newCall(request).execute().use {
                reportAuthStatus(it.code)
                it.isSuccessful
            }
        } catch (e: Exception) {
            false
        }
    }
}
