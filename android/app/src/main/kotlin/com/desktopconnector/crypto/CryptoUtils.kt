package com.desktopconnector.crypto

import org.bouncycastle.crypto.agreement.X25519Agreement
import org.bouncycastle.crypto.generators.HKDFBytesGenerator
import org.bouncycastle.crypto.params.HKDFParameters
import org.bouncycastle.crypto.params.X25519PrivateKeyParameters
import org.bouncycastle.crypto.params.X25519PublicKeyParameters
import org.bouncycastle.util.encoders.Hex
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec
import org.bouncycastle.crypto.digests.SHA256Digest

/**
 * Low-level crypto operations matching the Python desktop/src/crypto.py implementation.
 * Uses X25519 for key exchange, HKDF-SHA256 for key derivation, AES-256-GCM for encryption.
 */
object CryptoUtils {

    private val HKDF_SALT = "desktop-connector".toByteArray()
    private val HKDF_INFO = "aes-256-gcm-key".toByteArray()
    const val CHUNK_SIZE = 2 * 1024 * 1024 // 2 MB
    private const val NONCE_SIZE = 12
    private const val TAG_SIZE_BITS = 128

    /** Generate a new X25519 keypair. Returns (privateKey, publicKey) as raw 32-byte arrays. */
    fun generateKeypair(): Pair<ByteArray, ByteArray> {
        val privateParams = X25519PrivateKeyParameters(SecureRandom())
        val publicParams = privateParams.generatePublicKey()
        return Pair(privateParams.encoded, publicParams.encoded)
    }

    /** Compute the public key from a raw 32-byte private key. */
    fun publicKeyFromPrivate(privateKey: ByteArray): ByteArray {
        val privateParams = X25519PrivateKeyParameters(privateKey, 0)
        return privateParams.generatePublicKey().encoded
    }

    /** Compute device ID: first 32 hex chars of SHA-256 of the raw public key. */
    fun deviceIdFromPublicKey(publicKey: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(publicKey)
        return Hex.toHexString(digest).substring(0, 32)
    }

    /** X25519 ECDH + HKDF-SHA256 → 32-byte AES key. */
    fun deriveSharedKey(myPrivateKey: ByteArray, theirPublicKey: ByteArray): ByteArray {
        // X25519 ECDH
        val privateParams = X25519PrivateKeyParameters(myPrivateKey, 0)
        val publicParams = X25519PublicKeyParameters(theirPublicKey, 0)
        val agreement = X25519Agreement()
        agreement.init(privateParams)
        val sharedSecret = ByteArray(agreement.agreementSize)
        agreement.calculateAgreement(publicParams, sharedSecret, 0)

        // HKDF-SHA256
        val hkdf = HKDFBytesGenerator(SHA256Digest())
        hkdf.init(HKDFParameters(sharedSecret, HKDF_SALT, HKDF_INFO))
        val derivedKey = ByteArray(32)
        hkdf.generateBytes(derivedKey, 0, 32)
        return derivedKey
    }

    /** Verification code: first 3 bytes of SHA-256(shared_key) as XXX-XXX. */
    fun getVerificationCode(sharedKey: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(sharedKey)
        val num = ((digest[0].toInt() and 0xFF) shl 16 or
                   (digest[1].toInt() and 0xFF shl 8) or
                   (digest[2].toInt() and 0xFF)) % 1000000
        val code = String.format("%06d", num)
        return "${code.substring(0, 3)}-${code.substring(3)}"
    }

    /** AES-256-GCM encrypt. Returns nonce(12) + ciphertext + tag(16). */
    fun encryptBlob(plaintext: ByteArray, key: ByteArray, nonce: ByteArray? = null): ByteArray {
        val actualNonce = nonce ?: ByteArray(NONCE_SIZE).also { SecureRandom().nextBytes(it) }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        val keySpec = SecretKeySpec(key, "AES")
        val gcmSpec = GCMParameterSpec(TAG_SIZE_BITS, actualNonce)
        cipher.init(Cipher.ENCRYPT_MODE, keySpec, gcmSpec)
        val ciphertext = cipher.doFinal(plaintext)
        // Return nonce + ciphertext (which includes the tag appended by GCM)
        return actualNonce + ciphertext
    }

    /** AES-256-GCM decrypt. Expects nonce(12) + ciphertext + tag(16). */
    fun decryptBlob(blob: ByteArray, key: ByteArray): ByteArray {
        val nonce = blob.copyOfRange(0, NONCE_SIZE)
        val ciphertext = blob.copyOfRange(NONCE_SIZE, blob.size)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        val keySpec = SecretKeySpec(key, "AES")
        val gcmSpec = GCMParameterSpec(TAG_SIZE_BITS, nonce)
        cipher.init(Cipher.DECRYPT_MODE, keySpec, gcmSpec)
        return cipher.doFinal(ciphertext)
    }

    /** Derive per-chunk nonce: base_nonce XOR chunk_index (little-endian padded to 12 bytes). */
    fun makeChunkNonce(baseNonce: ByteArray, chunkIndex: Int): ByteArray {
        val indexBytes = ByteBuffer.allocate(NONCE_SIZE)
            .order(ByteOrder.LITTLE_ENDIAN)
            .putInt(chunkIndex)
            .array()
        // Pad to 12 bytes (ByteBuffer allocated 12, putInt writes 4, rest is 0)
        return ByteArray(NONCE_SIZE) { i -> (baseNonce[i].toInt() xor indexBytes[i].toInt()).toByte() }
    }
}
