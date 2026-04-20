package com.desktopconnector.crypto

import android.content.Context
import android.util.Base64
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONObject

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

    /** Drop every paired_* row. Shared helper used by the .fn.unpair flow
     *  and by the AUTH_INVALID banner's re-pair button. */
    fun removeAllPairedDevices() {
        val editor = securePrefs.edit()
        securePrefs.all.keys
            .filter { it.startsWith("paired_") }
            .forEach { editor.remove(it) }
        editor.apply()
    }

    /** For AUTH_INVALID kind = CREDENTIALS_INVALID: also nuke the on-device
     *  keypair so the next register() generates a fresh public key (and
     *  therefore a fresh device_id). The server's zombie row expires on its
     *  own. */
    fun resetKeypair() {
        securePrefs.edit().remove("private_key").apply()
        generateKeypair()
    }

    // --- File encryption (streaming) ---

    /** Generate a random 12-byte base nonce for a transfer. */
    fun generateBaseNonce(): ByteArray =
        ByteArray(12).also { java.security.SecureRandom().nextBytes(it) }

    /** Build and encrypt the transfer metadata blob (base64). */
    fun buildEncryptedMetadata(
        fileName: String,
        mimeType: String,
        fileSize: Long,
        chunkCount: Int,
        baseNonce: ByteArray,
        symmetricKey: ByteArray,
    ): String {
        val metadata = JSONObject().apply {
            put("filename", fileName)
            put("mime_type", mimeType)
            put("size", fileSize)
            put("chunk_count", chunkCount)
            put("chunk_size", CryptoUtils.CHUNK_SIZE)
            put("base_nonce", Base64.encodeToString(baseNonce, Base64.NO_WRAP))
        }
        val metaBlob = CryptoUtils.encryptBlob(metadata.toString().toByteArray(), symmetricKey)
        return Base64.encodeToString(metaBlob, Base64.NO_WRAP)
    }

    /** Encrypt a single plaintext chunk using the per-chunk derived nonce. */
    fun encryptChunk(
        plaintext: ByteArray,
        baseNonce: ByteArray,
        chunkIndex: Int,
        symmetricKey: ByteArray,
    ): ByteArray {
        val nonce = CryptoUtils.makeChunkNonce(baseNonce, chunkIndex)
        return CryptoUtils.encryptBlob(plaintext, symmetricKey, nonce)
    }
}

data class PairedDeviceInfo(
    val deviceId: String,
    val pubkeyB64: String,
    val symmetricKeyB64: String,
    val name: String,
    val pairedAt: Long,
)
