package com.desktopconnector.crypto

import android.content.Context
import android.util.Base64
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONObject
import java.io.InputStream

/**
 * Manages device identity keys and paired device keys.
 * Private key stored in EncryptedSharedPreferences (backed by Android Keystore).
 */
class KeyManager(context: Context) {

    private val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private val securePrefs = EncryptedSharedPreferences.create(
        context,
        "dc_secure_keys",
        masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
    )

    init {
        if (!securePrefs.contains("private_key")) {
            generateKeypair()
        }
    }

    private fun generateKeypair() {
        val (privateKey, _) = CryptoUtils.generateKeypair()
        securePrefs.edit()
            .putString("private_key", Base64.encodeToString(privateKey, Base64.NO_WRAP))
            .apply()
    }

    val privateKey: ByteArray
        get() = Base64.decode(securePrefs.getString("private_key", "")!!, Base64.NO_WRAP)

    val publicKey: ByteArray
        get() = CryptoUtils.publicKeyFromPrivate(privateKey)

    val publicKeyB64: String
        get() = Base64.encodeToString(publicKey, Base64.NO_WRAP)

    val deviceId: String
        get() = CryptoUtils.deviceIdFromPublicKey(publicKey)

    fun deriveSharedKey(theirPublicKeyB64: String): ByteArray {
        val theirPublicKey = Base64.decode(theirPublicKeyB64, Base64.NO_WRAP)
        return CryptoUtils.deriveSharedKey(privateKey, theirPublicKey)
    }

    fun getVerificationCode(sharedKey: ByteArray): String {
        return CryptoUtils.getVerificationCode(sharedKey)
    }

    // --- Paired device management ---

    fun savePairedDevice(deviceId: String, pubkeyB64: String, symmetricKeyB64: String, name: String) {
        val json = JSONObject().apply {
            put("pubkey", pubkeyB64)
            put("symmetric_key", symmetricKeyB64)
            put("name", name)
            put("paired_at", System.currentTimeMillis() / 1000)
        }
        securePrefs.edit()
            .putString("paired_$deviceId", json.toString())
            .apply()
    }

    fun getPairedDevice(deviceId: String): PairedDeviceInfo? {
        val jsonStr = securePrefs.getString("paired_$deviceId", null) ?: return null
        val json = JSONObject(jsonStr)
        return PairedDeviceInfo(
            deviceId = deviceId,
            pubkeyB64 = json.getString("pubkey"),
            symmetricKeyB64 = json.getString("symmetric_key"),
            name = json.optString("name", "Unknown"),
            pairedAt = json.optLong("paired_at", 0),
        )
    }

    fun getFirstPairedDevice(): PairedDeviceInfo? {
        return securePrefs.all.entries
            .filter { it.key.startsWith("paired_") }
            .firstNotNullOfOrNull { entry ->
                val did = entry.key.removePrefix("paired_")
                getPairedDevice(did)
            }
    }

    fun hasPairedDevice(): Boolean = getFirstPairedDevice() != null

    fun removePairedDevice(deviceId: String) {
        securePrefs.edit().remove("paired_$deviceId").apply()
    }

    // --- File encryption ---

    fun encryptFileToChunks(
        inputStream: InputStream,
        fileSize: Long,
        fileName: String,
        mimeType: String,
        symmetricKey: ByteArray,
    ): EncryptedFileResult {
        val data = inputStream.readBytes()
        val baseNonce = ByteArray(12).also { java.security.SecureRandom().nextBytes(it) }

        val chunks = mutableListOf<ByteArray>()
        var offset = 0
        var chunkIndex = 0
        while (offset < data.size) {
            val end = minOf(offset + CryptoUtils.CHUNK_SIZE, data.size)
            val chunkData = data.copyOfRange(offset, end)
            val nonce = CryptoUtils.makeChunkNonce(baseNonce, chunkIndex)
            chunks.add(CryptoUtils.encryptBlob(chunkData, symmetricKey, nonce))
            offset = end
            chunkIndex++
        }
        if (chunks.isEmpty()) {
            val nonce = CryptoUtils.makeChunkNonce(baseNonce, 0)
            chunks.add(CryptoUtils.encryptBlob(ByteArray(0), symmetricKey, nonce))
        }

        val metadata = JSONObject().apply {
            put("filename", fileName)
            put("mime_type", mimeType)
            put("size", fileSize)
            put("chunk_count", chunks.size)
            put("chunk_size", CryptoUtils.CHUNK_SIZE)
            put("base_nonce", Base64.encodeToString(baseNonce, Base64.NO_WRAP))
        }
        val metaBlob = CryptoUtils.encryptBlob(metadata.toString().toByteArray(), symmetricKey)
        val encryptedMeta = Base64.encodeToString(metaBlob, Base64.NO_WRAP)

        return EncryptedFileResult(encryptedMeta, chunks)
    }
}

data class PairedDeviceInfo(
    val deviceId: String,
    val pubkeyB64: String,
    val symmetricKeyB64: String,
    val name: String,
    val pairedAt: Long,
)

data class EncryptedFileResult(
    val encryptedMeta: String,
    val encryptedChunks: List<ByteArray>,
)
