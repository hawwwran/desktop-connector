<?php

/**
 * Vault-v1 cryptographic primitives — PHP twin of
 * ``desktop/src/vault_crypto.py``. Exact byte-for-byte parity with the
 * Python implementation is enforced by the cross-platform JSON test
 * vectors at ``tests/protocol/vault-v1/`` (see T2.4 vectors test).
 *
 * Required PHP extensions:
 *   - sodium (XChaCha20-Poly1305 + Argon2id + X25519)
 *   - hash   (HKDF; bundled in standard PHP)
 *
 * The ``intl`` extension is optional: when present, ``Normalizer`` is
 * used to NFC-normalize passphrases before hashing. When absent, the
 * passphrase is passed through as-is — fine for ASCII-only inputs but
 * breaks parity with the Python side for non-ASCII strings. Prod
 * deployments that accept arbitrary user passphrases SHOULD install
 * ``php-intl`` to avoid silent key divergence across platforms.
 */
class VaultCrypto
{
    public const ARGON2ID_MEMORY_KIB = 131072;     // 128 MiB
    public const ARGON2ID_ITERATIONS = 4;
    public const ARGON2ID_PARALLELISM = 1;
    public const ARGON2ID_SALT_BYTES = 16;

    public const XCHACHA20_KEY_BYTES = 32;
    public const XCHACHA20_NONCE_BYTES = 24;
    public const POLY1305_TAG_BYTES = 16;

    public const MASTER_KEY_BYTES = 32;

    /**
     * Plaintext format-version byte at the head of each versioned
     * envelope (manifest, header, recovery, device grant, export
     * outer). Per T0 §A3 / formats §7 it is plaintext (NOT in AAD) so
     * a reader can refuse a future version before AEAD. Chunk envelopes
     * intentionally omit this byte — chunk format is pinned by the
     * ``ch_v1_…`` chunk_id namespace (formats §11.1).
     */
    public const SUPPORTED_FORMAT_VERSION = 1;

    private const RECOVERY_WRAP_LABEL = 'dc-vault-v1/recovery-wrap';
    private const DEVICE_GRANT_WRAP_LABEL = 'dc-vault-v1/device-grant-wrap';

    // Schema strings — locked in formats §6.x; never trim, never re-case.
    private const MANIFEST_AAD_SCHEMA      = 'dc-vault-manifest-v1';      // 20 bytes
    private const CHUNK_AAD_SCHEMA         = 'dc-vault-chunk-v1';         // 17 bytes
    private const HEADER_AAD_SCHEMA        = 'dc-vault-header-v1';        // 18 bytes
    private const RECOVERY_AAD_SCHEMA      = 'dc-vault-recovery-v1';      // 20 bytes
    private const DEVICE_GRANT_AAD_SCHEMA  = 'dc-vault-device-grant-v1';  // 24 bytes
    private const EXPORT_MAGIC             = 'DCVE';                       // 4 bytes
    private const EXPORT_WRAP_AAD_SCHEMA   = 'dc-vault-export-wrap-v1';   // 23 bytes
    private const EXPORT_RECORD_AAD_SCHEMA = 'dc-vault-export-record-v1'; // 25 bytes

    // ---------------------------------------------------------------- HKDF

    /**
     * HKDF-SHA256 with ``salt = 32 zero bytes`` per RFC 5869 §2.2.
     * Mirror of ``vault_crypto.derive_subkey``.
     */
    public static function deriveSubkey(string $label, string $masterKey, int $length = 32): string
    {
        if (strlen($masterKey) !== self::MASTER_KEY_BYTES) {
            throw new InvalidArgumentException(
                'masterKey must be ' . self::MASTER_KEY_BYTES . ' bytes; got ' . strlen($masterKey)
            );
        }
        if ($length <= 0) {
            throw new InvalidArgumentException("length must be positive; got {$length}");
        }
        return hash_hkdf('sha256', $masterKey, $length, $label, str_repeat("\0", 32));
    }

    // ---------------------------------------------------------------- AEAD

    /**
     * XChaCha20-Poly1305-IETF encrypt. Returns ``ciphertext || 16-byte tag``.
     * Mirror of ``vault_crypto.aead_encrypt``.
     */
    public static function aeadEncrypt(string $plaintext, string $key, string $nonce, string $aad): string
    {
        if (strlen($key) !== self::XCHACHA20_KEY_BYTES) {
            throw new InvalidArgumentException(
                'key must be ' . self::XCHACHA20_KEY_BYTES . ' bytes; got ' . strlen($key)
            );
        }
        if (strlen($nonce) !== self::XCHACHA20_NONCE_BYTES) {
            throw new InvalidArgumentException(
                'nonce must be ' . self::XCHACHA20_NONCE_BYTES . ' bytes; got ' . strlen($nonce)
            );
        }
        return sodium_crypto_aead_xchacha20poly1305_ietf_encrypt(
            $plaintext, $aad, $nonce, $key
        );
    }

    /**
     * XChaCha20-Poly1305-IETF decrypt. Throws ``SodiumException`` on
     * AEAD verification failure (wrong key/nonce/AAD/tampered ciphertext
     * or tag). Mirror of ``vault_crypto.aead_decrypt``.
     */
    public static function aeadDecrypt(string $ciphertextAndTag, string $key, string $nonce, string $aad): string
    {
        if (strlen($key) !== self::XCHACHA20_KEY_BYTES) {
            throw new InvalidArgumentException(
                'key must be ' . self::XCHACHA20_KEY_BYTES . ' bytes; got ' . strlen($key)
            );
        }
        if (strlen($nonce) !== self::XCHACHA20_NONCE_BYTES) {
            throw new InvalidArgumentException(
                'nonce must be ' . self::XCHACHA20_NONCE_BYTES . ' bytes; got ' . strlen($nonce)
            );
        }
        if (strlen($ciphertextAndTag) < self::POLY1305_TAG_BYTES) {
            throw new InvalidArgumentException(
                'ciphertextAndTag must be at least ' . self::POLY1305_TAG_BYTES . ' bytes (the tag)'
            );
        }
        // libsodium returns false on AEAD failure in PHP; raise to match
        // PyNaCl's CryptoError semantics so the test harness can catch
        // both with the same expectation.
        $plaintext = sodium_crypto_aead_xchacha20poly1305_ietf_decrypt(
            $ciphertextAndTag, $aad, $nonce, $key
        );
        if ($plaintext === false) {
            throw new SodiumException('XChaCha20-Poly1305 verification failed');
        }
        return $plaintext;
    }

    // ---------------------------------------------------------------- Argon2id

    /**
     * Argon2id KDF with the v1-locked params (m=128 MiB, t=4, p=1 — libsodium
     * fixes p=1 internally). Passphrase is NFC-normalized when ``intl`` is
     * available; otherwise it's passed through as UTF-8 bytes.
     *
     * Mirror of ``vault_crypto.argon2id_kdf``.
     */
    public static function argon2idKdf(
        string $passphrase,
        string $salt,
        int $outputLength = 32,
        int $memoryKib = self::ARGON2ID_MEMORY_KIB,
        int $iterations = self::ARGON2ID_ITERATIONS,
    ): string {
        if (strlen($salt) !== self::ARGON2ID_SALT_BYTES) {
            throw new InvalidArgumentException(
                'salt must be ' . self::ARGON2ID_SALT_BYTES . ' bytes; got ' . strlen($salt)
            );
        }
        if ($outputLength <= 0) {
            throw new InvalidArgumentException("outputLength must be positive; got {$outputLength}");
        }
        if ($memoryKib <= 0) {
            throw new InvalidArgumentException("memoryKib must be positive; got {$memoryKib}");
        }
        if ($iterations <= 0) {
            throw new InvalidArgumentException("iterations must be positive; got {$iterations}");
        }

        $pwBytes = self::nfcNormalize($passphrase);
        $memlimitBytes = $memoryKib * 1024;

        return sodium_crypto_pwhash(
            $outputLength,
            $pwBytes,
            $salt,
            $iterations,
            $memlimitBytes,
            SODIUM_CRYPTO_PWHASH_ALG_ARGON2ID13
        );
    }

    private static function nfcNormalize(string $s): string
    {
        if (class_exists('Normalizer')) {
            return Normalizer::normalize($s, Normalizer::FORM_C);
        }
        return $s;   // ASCII-safe passthrough
    }

    // ---------------------------------------------------------------- ID normalization

    public static function normalizeVaultId(string $vaultId): string
    {
        return strtoupper(str_replace('-', '', $vaultId));
    }

    // ---------------------------------------------------------------- Format-version guard

    /**
     * Stop before AEAD when the format-version byte of an envelope is
     * unknown. Mirrors ``vault_crypto.assert_supported_format_version``.
     *
     * @throws VaultFormatVersionUnsupportedError 422 envelope when the
     *     byte at ``$offset`` is not 0x01.
     * @throws InvalidArgumentException when ``$envelope`` is too short
     *     to hold the format-version byte at the requested offset.
     */
    public static function assertSupportedFormatVersion(
        string $envelope,
        string $kind,
        int $offset = 0,
    ): void {
        if (strlen($envelope) <= $offset) {
            throw new InvalidArgumentException(
                "{$kind} envelope too short for format-version byte at offset {$offset}"
            );
        }
        $observed = ord($envelope[$offset]);
        if ($observed !== self::SUPPORTED_FORMAT_VERSION) {
            throw new VaultFormatVersionUnsupportedError($kind, $observed);
        }
    }

    // ---------------------------------------------------------------- Manifest AAD + envelope

    public static function buildManifestAad(
        string $vaultId,
        int $revision,
        int $parentRevision,
        string $authorDeviceId,
    ): string {
        $canonical = self::normalizeVaultId($vaultId);
        if (strlen($canonical) !== 12) {
            throw new InvalidArgumentException("vault_id must canonicalize to 12 bytes");
        }
        if (strlen($authorDeviceId) !== 32) {
            throw new InvalidArgumentException("author_device_id must be 32 hex chars");
        }
        return self::MANIFEST_AAD_SCHEMA
            . $canonical
            . self::packBeU64($revision)
            . self::packBeU64($parentRevision)
            . $authorDeviceId;
    }

    public static function buildManifestEnvelope(
        string $vaultId,
        int $revision,
        int $parentRevision,
        string $authorDeviceId,
        string $nonce,
        string $aeadCiphertextAndTag,
        int $formatVersion = 1,
    ): string {
        if ($formatVersion < 0 || $formatVersion > 0xFF) {
            throw new InvalidArgumentException("format_version must fit in u8");
        }
        if (strlen($nonce) !== self::XCHACHA20_NONCE_BYTES) {
            throw new InvalidArgumentException("nonce must be 24 bytes");
        }
        $canonical = self::normalizeVaultId($vaultId);
        return chr($formatVersion)
            . $canonical
            . self::packBeU64($revision)
            . self::packBeU64($parentRevision)
            . $authorDeviceId
            . $nonce
            . $aeadCiphertextAndTag;
    }

    /**
     * Parse the first 61 bytes of a manifest envelope (formats §10.1).
     * The relay uses this to authoritatively read revision / parent_revision
     * / author_device_id from the envelope-internal AAD source rather than
     * trusting the JSON body — a buggy or malicious caller whose envelope
     * disagrees with body fields would otherwise poison the manifest chain.
     *
     * Returns ['format_version', 'vault_id', 'revision', 'parent_revision',
     * 'author_device_id'] on success. Throws InvalidArgumentException for
     * any malformed prefix (too short, vault_id not base32, device id not
     * hex). Does NOT verify the AEAD; that's the receiver's job.
     */
    public static function parseManifestEnvelopeHeader(string $envelope): array
    {
        if (strlen($envelope) < 61) {
            throw new InvalidArgumentException(
                'manifest envelope is shorter than the 61-byte deterministic prefix'
            );
        }
        $formatVersion = ord($envelope[0]);
        $vaultIdBytes = substr($envelope, 1, 12);
        if (!preg_match('/^[A-Z2-7]{12}$/', $vaultIdBytes)) {
            throw new InvalidArgumentException('manifest envelope vault_id is not base32');
        }
        $revisionRaw = substr($envelope, 13, 8);
        $parentRaw   = substr($envelope, 21, 8);
        $authorRaw   = substr($envelope, 29, 32);
        if (!preg_match('/^[a-f0-9]{32}$/', $authorRaw)) {
            throw new InvalidArgumentException(
                'manifest envelope author_device_id is not 32 lowercase hex chars'
            );
        }
        $unpackedRev    = unpack('J', $revisionRaw);
        $unpackedParent = unpack('J', $parentRaw);
        return [
            'format_version'   => $formatVersion,
            'vault_id'         => $vaultIdBytes,
            'revision'         => (int)$unpackedRev[1],
            'parent_revision'  => (int)$unpackedParent[1],
            'author_device_id' => $authorRaw,
        ];
    }

    /**
     * Parse the first 21 bytes of a header envelope (formats §9.1):
     * format_version(1) + vault_id(12) + header_revision_be64(8). Same
     * idea as ``parseManifestEnvelopeHeader`` — the relay sources
     * (header_revision, vault_id) from the envelope rather than the body.
     */
    public static function parseHeaderEnvelopeHeader(string $envelope): array
    {
        if (strlen($envelope) < 21) {
            throw new InvalidArgumentException(
                'header envelope is shorter than the 21-byte deterministic prefix'
            );
        }
        $formatVersion = ord($envelope[0]);
        $vaultIdBytes = substr($envelope, 1, 12);
        if (!preg_match('/^[A-Z2-7]{12}$/', $vaultIdBytes)) {
            throw new InvalidArgumentException('header envelope vault_id is not base32');
        }
        $unpackedRev = unpack('J', substr($envelope, 13, 8));
        return [
            'format_version'  => $formatVersion,
            'vault_id'        => $vaultIdBytes,
            'header_revision' => (int)$unpackedRev[1],
        ];
    }

    // ---------------------------------------------------------------- Chunk AAD + envelope

    public static function buildChunkAad(
        string $vaultId,
        string $remoteFolderId,
        string $fileId,
        string $fileVersionId,
        int $chunkIndex,
        int $chunkPlaintextSize,
    ): string {
        $canonical = self::normalizeVaultId($vaultId);
        foreach ([$remoteFolderId, $fileId, $fileVersionId] as $id) {
            if (strlen($id) !== 30) {
                throw new InvalidArgumentException("id must be 30 bytes; got {$id}");
            }
        }
        return self::CHUNK_AAD_SCHEMA
            . $canonical
            . $remoteFolderId . $fileId . $fileVersionId
            . self::packBeU64($chunkIndex)
            . self::packBeU64($chunkPlaintextSize);
    }

    public static function buildChunkEnvelope(string $nonce, string $aeadCiphertextAndTag): string
    {
        if (strlen($nonce) !== self::XCHACHA20_NONCE_BYTES) {
            throw new InvalidArgumentException("nonce must be 24 bytes");
        }
        return $nonce . $aeadCiphertextAndTag;
    }

    // ---------------------------------------------------------------- Header AAD + envelope

    public static function buildHeaderAad(string $vaultId, int $headerRevision): string
    {
        $canonical = self::normalizeVaultId($vaultId);
        return self::HEADER_AAD_SCHEMA . $canonical . self::packBeU64($headerRevision);
    }

    public static function buildHeaderEnvelope(
        string $vaultId,
        int $headerRevision,
        string $nonce,
        string $aeadCiphertextAndTag,
        int $formatVersion = 1,
    ): string {
        return chr($formatVersion)
            . self::normalizeVaultId($vaultId)
            . self::packBeU64($headerRevision)
            . $nonce
            . $aeadCiphertextAndTag;
    }

    // ---------------------------------------------------------------- Recovery envelope

    public static function buildRecoveryAad(string $vaultId, string $envelopeId): string
    {
        $canonical = self::normalizeVaultId($vaultId);
        if (strlen($envelopeId) !== 30) {
            throw new InvalidArgumentException("envelope_id must be 30 bytes");
        }
        return self::RECOVERY_AAD_SCHEMA . $canonical . $envelopeId;
    }

    public static function deriveRecoveryWrapKey(
        string $passphrase,
        string $recoverySecret,
        string $argonSalt,
        int $memoryKib = self::ARGON2ID_MEMORY_KIB,
        int $iterations = self::ARGON2ID_ITERATIONS,
    ): string {
        if (strlen($recoverySecret) !== 32) {
            throw new InvalidArgumentException("recovery_secret must be 32 bytes");
        }
        $argonOut = self::argon2idKdf($passphrase, $argonSalt, 32, $memoryKib, $iterations);
        return hash_hkdf('sha256', $recoverySecret, 32, self::RECOVERY_WRAP_LABEL, $argonOut);
    }

    public static function buildRecoveryEnvelope(
        string $vaultId,
        string $envelopeId,
        string $argonSalt,
        string $nonce,
        string $aeadCiphertextAndTag,
        int $formatVersion = 1,
    ): string {
        if (strlen($argonSalt) !== self::ARGON2ID_SALT_BYTES) {
            throw new InvalidArgumentException("argon_salt must be 16 bytes");
        }
        if (strlen($nonce) !== self::XCHACHA20_NONCE_BYTES) {
            throw new InvalidArgumentException("nonce must be 24 bytes");
        }
        if (strlen($envelopeId) !== 30) {
            throw new InvalidArgumentException("envelope_id must be 30 bytes");
        }
        return chr($formatVersion)
            . self::normalizeVaultId($vaultId)
            . $envelopeId
            . $argonSalt
            . $nonce
            . $aeadCiphertextAndTag;
    }

    // ---------------------------------------------------------------- Device grant

    public static function buildDeviceGrantAad(
        string $vaultId,
        string $grantId,
        string $claimantDeviceId,
    ): string {
        $canonical = self::normalizeVaultId($vaultId);
        if (strlen($grantId) !== 30) {
            throw new InvalidArgumentException("grant_id must be 30 bytes");
        }
        if (strlen($claimantDeviceId) !== 32) {
            throw new InvalidArgumentException("claimant_device_id must be 32 hex chars");
        }
        return self::DEVICE_GRANT_AAD_SCHEMA . $canonical . $grantId . $claimantDeviceId;
    }

    public static function deriveDeviceGrantWrapKey(string $sharedSecret): string
    {
        if (strlen($sharedSecret) !== 32) {
            throw new InvalidArgumentException("shared_secret must be 32 bytes");
        }
        return hash_hkdf('sha256', $sharedSecret, 32, self::DEVICE_GRANT_WRAP_LABEL, str_repeat("\0", 32));
    }

    public static function buildDeviceGrantEnvelope(
        string $vaultId,
        string $grantId,
        string $claimantPubkey,
        string $nonce,
        string $aeadCiphertextAndTag,
        int $formatVersion = 1,
    ): string {
        if (strlen($claimantPubkey) !== 32) {
            throw new InvalidArgumentException("claimant_pubkey must be 32 bytes");
        }
        if (strlen($nonce) !== self::XCHACHA20_NONCE_BYTES) {
            throw new InvalidArgumentException("nonce must be 24 bytes");
        }
        if (strlen($grantId) !== 30) {
            throw new InvalidArgumentException("grant_id must be 30 bytes");
        }
        return chr($formatVersion)
            . self::normalizeVaultId($vaultId)
            . $grantId
            . $claimantPubkey
            . $nonce
            . $aeadCiphertextAndTag;
    }

    // ---------------------------------------------------------------- Export bundle

    public static function buildExportOuterHeader(
        int $argonMemoryKib,
        int $argonIterations,
        int $argonParallelism,
        string $argonSalt,
        string $outerNonce,
        int $formatVersion = 1,
    ): string {
        if (strlen($argonSalt) !== 16) {
            throw new InvalidArgumentException("argon_salt must be 16 bytes");
        }
        if (strlen($outerNonce) !== self::XCHACHA20_NONCE_BYTES) {
            throw new InvalidArgumentException("outer_nonce must be 24 bytes");
        }
        return self::EXPORT_MAGIC
            . chr($formatVersion)
            . self::packBeU32($argonMemoryKib)
            . self::packBeU32($argonIterations)
            . self::packBeU32($argonParallelism)
            . $argonSalt
            . $outerNonce;
    }

    public static function buildExportWrapAad(string $vaultId): string
    {
        return self::EXPORT_WRAP_AAD_SCHEMA . self::normalizeVaultId($vaultId);
    }

    public static function buildExportRecordAad(string $vaultId, int $recordIndex, int $recordType): string
    {
        return self::EXPORT_RECORD_AAD_SCHEMA
            . self::normalizeVaultId($vaultId)
            . self::packBeU32($recordIndex)
            . chr($recordType);
    }

    public static function deriveExportWrapKey(
        string $passphrase,
        string $argonSalt,
        int $memoryKib = self::ARGON2ID_MEMORY_KIB,
        int $iterations = self::ARGON2ID_ITERATIONS,
    ): string {
        return self::argon2idKdf($passphrase, $argonSalt, 32, $memoryKib, $iterations);
    }

    // ---------------------------------------------------------------- byte packing helpers

    private static function packBeU64(int $n): string
    {
        if ($n < 0) {
            throw new InvalidArgumentException("u64 must be non-negative; got {$n}");
        }
        // pack('J', ...) is big-endian unsigned 64-bit on PHP 7+.
        return pack('J', $n);
    }

    private static function packBeU32(int $n): string
    {
        if ($n < 0 || $n > 0xFFFFFFFF) {
            throw new InvalidArgumentException("u32 out of range; got {$n}");
        }
        return pack('N', $n);
    }
}
