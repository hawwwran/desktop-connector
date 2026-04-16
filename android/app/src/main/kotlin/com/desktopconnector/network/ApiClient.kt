package com.desktopconnector.network

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

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

    fun setCredentials(deviceId: String, authToken: String) {
        this.deviceId = deviceId
        this.authToken = authToken
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

    fun initTransfer(transferId: String, recipientId: String, encryptedMeta: String, chunkCount: Int): Boolean {
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
        return try {
            val request = authHeaders(Request.Builder()).url("$serverUrl/api/health").get().build()
            val c = if (fast) heartbeatClient else client
            c.newCall(request).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            false
        }
    }

    private fun executeJson(request: Request): JSONObject? {
        return try {
            client.newCall(request).execute().use { resp ->
                val body = resp.body?.string() ?: return null
                if (resp.isSuccessful) JSONObject(body) else null
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun executeStatus(request: Request): Boolean {
        return try {
            client.newCall(request).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            false
        }
    }
}
