<?php

declare(strict_types=1);

use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

/**
 * Cross-platform test-vector harness, PHP side (T2.4).
 *
 * Loads the same JSON files at ``tests/protocol/vault-v1/`` that the
 * Python harness (``tests/protocol/test_vault_v1_vectors.py``) consumes
 * and runs each case through the PHP twin (``VaultCrypto``). Byte-exact
 * parity between the two runtimes is the build gate: if any case
 * produces different bytes on either side, the build breaks.
 *
 * Schema lock: T0 §A18.
 *
 * Per-primitive runners mirror the Python ``_run_*_case`` helpers. The
 * positive-case shape is:
 *   1. Compute every intermediate (AAD, subkey/wrap_key, ciphertext, envelope).
 *   2. assertSame the expected hex at every step.
 *   3. Round-trip-decrypt to recover the original plaintext.
 *
 * Negative cases apply the case's ``tamper`` directives and assert the
 * decryption raises ``SodiumException`` (the PHP equivalent of PyNaCl's
 * ``CryptoError``).
 */
final class VaultCryptoVectorsTest extends TestCase
{
    private const VECTORS_DIR = __DIR__ . '/../../../tests/protocol/vault-v1';

    /** @return list<array{0: string, 1: array}> [name, case] tuples for data providers. */
    private static function loadCases(string $filename): array
    {
        $path = self::VECTORS_DIR . '/' . $filename;
        $raw = file_get_contents($path);
        if ($raw === false) {
            throw new RuntimeException("missing vector file: {$path}");
        }
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            throw new RuntimeException("vector file {$filename} is not a JSON array");
        }
        $out = [];
        foreach ($data as $case) {
            $out[] = [$case['name'], $case];
        }
        return $out;
    }

    // ---------------------------------------------------------------- chunk

    public static function chunkCases(): array { return self::loadCases('chunk_v1.json'); }

    #[DataProvider("chunkCases")]
    public function test_chunk_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];

        $masterKey = hex2bin($inputs['vault_master_key']);
        $nonce = hex2bin($inputs['nonce']);
        $plaintext = base64_decode($inputs['chunk_plaintext'], true);

        $aad = VaultCrypto::buildChunkAad(
            $inputs['vault_id'],
            $inputs['remote_folder_id'],
            $inputs['file_id'],
            $inputs['file_version_id'],
            (int)$inputs['chunk_index'],
            (int)$inputs['chunk_plaintext_size']
        );
        $subkey = VaultCrypto::deriveSubkey('dc-vault-v1/chunk', $masterKey);
        $ct = VaultCrypto::aeadEncrypt($plaintext, $subkey, $nonce, $aad);
        $envelope = VaultCrypto::buildChunkEnvelope($nonce, $ct);

        if (isset($expected['expected_error'])) {
            $tamper = $case['tamper'] ?? [];
            $decryptAad = $aad;
            $decryptEnvelope = $envelope;
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $decryptEnvelope[$spec['offset']] = chr(
                    ord($decryptEnvelope[$spec['offset']]) ^ hexdec($spec['xor'])
                );
            }
            if (isset($tamper['aad_override'])) {
                $decryptAad = hex2bin($tamper['aad_override']);
            }
            $this->expectException(SodiumException::class);
            VaultCrypto::aeadDecrypt(substr($decryptEnvelope, 24), $subkey, $nonce, $decryptAad);
            return;
        }

        self::assertSame($expected['aad'], bin2hex($aad));
        self::assertSame($expected['subkey'], bin2hex($subkey));
        self::assertSame($expected['aead_ciphertext_and_tag'], bin2hex($ct));
        self::assertSame($expected['envelope_bytes'], bin2hex($envelope));
        self::assertSame($plaintext, VaultCrypto::aeadDecrypt($ct, $subkey, $nonce, $aad));
    }

    // ---------------------------------------------------------------- header

    public static function headerCases(): array { return self::loadCases('header_v1.json'); }

    #[DataProvider("headerCases")]
    public function test_header_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];

        $masterKey = hex2bin($inputs['vault_master_key']);
        $nonce = hex2bin($inputs['nonce']);
        $plaintext = base64_decode($inputs['header_plaintext'], true);
        $rev = (int)$inputs['header_revision'];

        $aad = VaultCrypto::buildHeaderAad($inputs['vault_id'], $rev);
        $subkey = VaultCrypto::deriveSubkey('dc-vault-v1/header', $masterKey);
        $ct = VaultCrypto::aeadEncrypt($plaintext, $subkey, $nonce, $aad);
        $envelope = VaultCrypto::buildHeaderEnvelope($inputs['vault_id'], $rev, $nonce, $ct);

        if (isset($expected['expected_error'])) {
            $tamper = $case['tamper'] ?? [];
            $decryptAad = $aad;
            $decryptEnvelope = $envelope;
            $decryptCt = $ct;
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $buf = $envelope;
                $buf[$spec['offset']] = chr(ord($buf[$spec['offset']]) ^ hexdec($spec['xor']));
                $decryptEnvelope = $buf;
                // Header envelope: 1+12+8 = 21 bytes plaintext header, +24 nonce.
                $decryptCt = substr($buf, 21 + 24);
            }
            if (isset($tamper['aad_override'])) {
                $decryptAad = hex2bin($tamper['aad_override']);
            }
            if ($expected['expected_error'] === 'vault_format_version_unsupported') {
                $this->expectException(VaultFormatVersionUnsupportedError::class);
                VaultCrypto::assertSupportedFormatVersion($decryptEnvelope, 'header');
                return;
            }
            $this->expectException(SodiumException::class);
            VaultCrypto::aeadDecrypt($decryptCt, $subkey, $nonce, $decryptAad);
            return;
        }

        self::assertSame($expected['aad'], bin2hex($aad));
        self::assertSame($expected['subkey'], bin2hex($subkey));
        self::assertSame($expected['aead_ciphertext_and_tag'], bin2hex($ct));
        self::assertSame($expected['envelope_bytes'], bin2hex($envelope));
        self::assertSame($plaintext, VaultCrypto::aeadDecrypt($ct, $subkey, $nonce, $aad));
    }

    // ---------------------------------------------------------------- root envelope

    /**
     * Review §7.C1: cross-runtime parity for ``root_v1.json``. Mirrors
     * the Python ``_run_root_case`` byte-for-byte so a regression in
     * either runtime's root-envelope builder or AAD constructor breaks
     * the build (was Python-only pre-fix).
     *
     * Root envelope plaintext header: 1+12+8+8+32+24 = 85 bytes (the
     * 32-byte author_device_id is hex-encoded so its raw form is 16
     * bytes, but the spec records the hex bytes in the envelope —
     * vault-v1-formats §10.1).
     */
    public static function rootCases(): array { return self::loadCases('root_v1.json'); }

    #[DataProvider("rootCases")]
    public function test_root_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];

        $masterKey = hex2bin($inputs['vault_master_key']);
        $nonce = hex2bin($inputs['nonce']);
        $plaintext = base64_decode($inputs['root_plaintext'], true);
        $rootRevision = (int)$inputs['root_revision'];
        $parentRevision = (int)$inputs['parent_root_revision'];

        $aad = VaultCrypto::buildRootAad(
            $inputs['vault_id'],
            $rootRevision,
            $parentRevision,
            $inputs['author_device_id']
        );
        $subkey = VaultCrypto::deriveSubkey('dc-vault-v1/root', $masterKey);
        $ct = VaultCrypto::aeadEncrypt($plaintext, $subkey, $nonce, $aad);
        $envelope = VaultCrypto::buildRootEnvelope(
            $inputs['vault_id'],
            $rootRevision,
            $parentRevision,
            $inputs['author_device_id'],
            $nonce,
            $ct
        );

        if (isset($expected['expected_error'])) {
            $tamper = $case['tamper'] ?? [];
            $decryptAad = $aad;
            $decryptEnvelope = $envelope;
            $decryptCt = $ct;
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $buf = $envelope;
                $buf[$spec['offset']] = chr(
                    ord($buf[$spec['offset']]) ^ hexdec($spec['xor'])
                );
                $decryptEnvelope = $buf;
                // Root envelope plaintext header is 85 bytes
                // (1+12+8+8+32+24); ciphertext starts at offset 85.
                $decryptCt = substr($buf, 85);
            }
            if (isset($tamper['aad_override'])) {
                $decryptAad = hex2bin($tamper['aad_override']);
            }
            if ($expected['expected_error'] === 'vault_format_version_unsupported') {
                $this->expectException(VaultFormatVersionUnsupportedError::class);
                VaultCrypto::assertSupportedFormatVersion($decryptEnvelope, 'root');
                return;
            }
            $this->expectException(SodiumException::class);
            VaultCrypto::aeadDecrypt($decryptCt, $subkey, $nonce, $decryptAad);
            return;
        }

        if (isset($expected['aad'])) {
            self::assertSame($expected['aad'], bin2hex($aad), "{$name}: AAD mismatch");
        }
        if (isset($expected['subkey'])) {
            self::assertSame($expected['subkey'], bin2hex($subkey), "{$name}: subkey mismatch");
        }
        if (isset($expected['aead_ciphertext_and_tag'])) {
            self::assertSame(
                $expected['aead_ciphertext_and_tag'], bin2hex($ct),
                "{$name}: ciphertext mismatch",
            );
        }
        if (isset($expected['envelope_bytes'])) {
            self::assertSame(
                $expected['envelope_bytes'], bin2hex($envelope),
                "{$name}: envelope mismatch",
            );
        }
        self::assertSame(
            $plaintext, VaultCrypto::aeadDecrypt($ct, $subkey, $nonce, $aad),
            "{$name}: round-trip plaintext mismatch",
        );
    }

    // ---------------------------------------------------------------- shard envelope

    /**
     * Review §7.C1: cross-runtime parity for ``shard_v1.json``. Same
     * pattern as root but the envelope plaintext header is 115 bytes
     * (the extra 30 bytes are the remote_folder_id) so the ciphertext
     * offset is 115.
     */
    public static function shardCases(): array { return self::loadCases('shard_v1.json'); }

    #[DataProvider("shardCases")]
    public function test_shard_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];

        $masterKey = hex2bin($inputs['vault_master_key']);
        $nonce = hex2bin($inputs['nonce']);
        $plaintext = base64_decode($inputs['shard_plaintext'], true);
        $shardRevision = (int)$inputs['shard_revision'];
        $parentShardRevision = (int)$inputs['parent_shard_revision'];

        $aad = VaultCrypto::buildShardAad(
            $inputs['vault_id'],
            $inputs['remote_folder_id'],
            $shardRevision,
            $parentShardRevision,
            $inputs['author_device_id']
        );
        $subkey = VaultCrypto::deriveSubkey('dc-vault-v1/shard', $masterKey);
        $ct = VaultCrypto::aeadEncrypt($plaintext, $subkey, $nonce, $aad);
        $envelope = VaultCrypto::buildShardEnvelope(
            $inputs['vault_id'],
            $inputs['remote_folder_id'],
            $shardRevision,
            $parentShardRevision,
            $inputs['author_device_id'],
            $nonce,
            $ct
        );

        if (isset($expected['expected_error'])) {
            $tamper = $case['tamper'] ?? [];
            $decryptAad = $aad;
            $decryptEnvelope = $envelope;
            $decryptCt = $ct;
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $buf = $envelope;
                $buf[$spec['offset']] = chr(
                    ord($buf[$spec['offset']]) ^ hexdec($spec['xor'])
                );
                $decryptEnvelope = $buf;
                // Shard envelope plaintext header is 115 bytes
                // (1+12+30+8+8+32+24); ciphertext starts at offset 115.
                $decryptCt = substr($buf, 115);
            }
            if (isset($tamper['aad_override'])) {
                $decryptAad = hex2bin($tamper['aad_override']);
            }
            if ($expected['expected_error'] === 'vault_format_version_unsupported') {
                $this->expectException(VaultFormatVersionUnsupportedError::class);
                VaultCrypto::assertSupportedFormatVersion($decryptEnvelope, 'shard');
                return;
            }
            $this->expectException(SodiumException::class);
            VaultCrypto::aeadDecrypt($decryptCt, $subkey, $nonce, $decryptAad);
            return;
        }

        if (isset($expected['aad'])) {
            self::assertSame($expected['aad'], bin2hex($aad), "{$name}: AAD mismatch");
        }
        if (isset($expected['subkey'])) {
            self::assertSame($expected['subkey'], bin2hex($subkey), "{$name}: subkey mismatch");
        }
        if (isset($expected['aead_ciphertext_and_tag'])) {
            self::assertSame(
                $expected['aead_ciphertext_and_tag'], bin2hex($ct),
                "{$name}: ciphertext mismatch",
            );
        }
        if (isset($expected['envelope_bytes'])) {
            self::assertSame(
                $expected['envelope_bytes'], bin2hex($envelope),
                "{$name}: envelope mismatch",
            );
        }
        self::assertSame(
            $plaintext, VaultCrypto::aeadDecrypt($ct, $subkey, $nonce, $aad),
            "{$name}: round-trip plaintext mismatch",
        );
    }

    // ---------------------------------------------------------------- recovery envelope

    public static function recoveryCases(): array { return self::loadCases('recovery_envelope_v1.json'); }

    #[DataProvider("recoveryCases")]
    public function test_recovery_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];

        $masterKey = hex2bin($inputs['vault_master_key']);
        $salt = hex2bin($inputs['argon_salt']);
        $nonce = hex2bin($inputs['nonce']);
        $secret = hex2bin($inputs['recovery_secret']);
        $memKib = (int)$inputs['argon_memory_kib'];
        $iters = (int)$inputs['argon_iterations'];

        $aad = VaultCrypto::buildRecoveryAad($inputs['vault_id'], $inputs['envelope_id']);
        $wrapKey = VaultCrypto::deriveRecoveryWrapKey(
            $inputs['passphrase'], $secret, $salt, $memKib, $iters
        );
        $ct = VaultCrypto::aeadEncrypt($masterKey, $wrapKey, $nonce, $aad);
        $envelope = VaultCrypto::buildRecoveryEnvelope(
            $inputs['vault_id'], $inputs['envelope_id'], $salt, $nonce, $ct
        );

        if (isset($expected['expected_error'])) {
            $decryptWrapKey = $wrapKey;
            $decryptEnvelope = $envelope;
            $decryptCt = $ct;
            if (isset($inputs['decrypt_passphrase_override'])) {
                $decryptWrapKey = VaultCrypto::deriveRecoveryWrapKey(
                    $inputs['decrypt_passphrase_override'], $secret, $salt, $memKib, $iters
                );
            }
            $tamper = $case['tamper'] ?? [];
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $buf = $envelope;
                $buf[$spec['offset']] = chr(ord($buf[$spec['offset']]) ^ hexdec($spec['xor']));
                $decryptEnvelope = $buf;
                // Recovery envelope plaintext header = 1+12+30+16+24 = 83 bytes.
                $decryptCt = substr($buf, 83);
            }
            if ($expected['expected_error'] === 'vault_format_version_unsupported') {
                $this->expectException(VaultFormatVersionUnsupportedError::class);
                VaultCrypto::assertSupportedFormatVersion($decryptEnvelope, 'recovery');
                return;
            }
            $this->expectException(SodiumException::class);
            VaultCrypto::aeadDecrypt($decryptCt, $decryptWrapKey, $nonce, $aad);
            return;
        }

        self::assertSame($expected['aad'], bin2hex($aad));
        self::assertSame($expected['wrap_key'], bin2hex($wrapKey));
        self::assertSame($expected['aead_ciphertext_and_tag'], bin2hex($ct));
        self::assertSame($expected['envelope_bytes'], bin2hex($envelope));
        self::assertSame($masterKey, VaultCrypto::aeadDecrypt($ct, $wrapKey, $nonce, $aad));
    }

    // ---------------------------------------------------------------- device grant

    public static function deviceGrantCases(): array { return self::loadCases('device_grant_v1.json'); }

    #[DataProvider("deviceGrantCases")]
    public function test_device_grant_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];

        $nonce = hex2bin($inputs['nonce']);
        $plaintext = base64_decode($inputs['grant_plaintext'], true);
        $adminPriv = hex2bin($inputs['admin_priv_seed']);
        $claimantPub = hex2bin($inputs['claimant_pubkey']);
        $sharedSecret = sodium_crypto_scalarmult($adminPriv, $claimantPub);
        self::assertSame($inputs['shared_secret'], bin2hex($sharedSecret),
            "{$name}: PHP X25519 disagrees with Python — wire-format break");

        $aad = VaultCrypto::buildDeviceGrantAad(
            $inputs['vault_id'], $inputs['grant_id'], $inputs['claimant_device_id']
        );
        $wrapKey = VaultCrypto::deriveDeviceGrantWrapKey($sharedSecret);
        $ct = VaultCrypto::aeadEncrypt($plaintext, $wrapKey, $nonce, $aad);
        $envelope = VaultCrypto::buildDeviceGrantEnvelope(
            $inputs['vault_id'], $inputs['grant_id'], $claimantPub, $nonce, $ct
        );

        if (isset($expected['expected_error'])) {
            $tamper = $case['tamper'] ?? [];
            $decryptAad = $aad;
            $decryptEnvelope = $envelope;
            $decryptCt = $ct;
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $buf = $envelope;
                $buf[$spec['offset']] = chr(ord($buf[$spec['offset']]) ^ hexdec($spec['xor']));
                $decryptEnvelope = $buf;
                // Device grant: 1+12+30+32 = 75 bytes header + 24 nonce.
                $decryptCt = substr($buf, 75 + 24);
            }
            if (isset($tamper['aad_override'])) {
                $decryptAad = hex2bin($tamper['aad_override']);
            }
            if ($expected['expected_error'] === 'vault_format_version_unsupported') {
                $this->expectException(VaultFormatVersionUnsupportedError::class);
                VaultCrypto::assertSupportedFormatVersion($decryptEnvelope, 'device_grant');
                return;
            }
            $this->expectException(SodiumException::class);
            VaultCrypto::aeadDecrypt($decryptCt, $wrapKey, $nonce, $decryptAad);
            return;
        }

        self::assertSame($expected['aad'], bin2hex($aad));
        self::assertSame($expected['wrap_key'], bin2hex($wrapKey));
        self::assertSame($expected['aead_ciphertext_and_tag'], bin2hex($ct));
        self::assertSame($expected['envelope_bytes'], bin2hex($envelope));
        self::assertSame($plaintext, VaultCrypto::aeadDecrypt($ct, $wrapKey, $nonce, $aad));
    }

    // ---------------------------------------------------------------- content fingerprint

    public static function contentFingerprintCases(): array
    {
        return self::loadCases('content_fingerprint_v1.json');
    }

    /**
     * F-T10: PHP twin of the Python ``_run_content_fingerprint_case``.
     * The relay doesn't compute fingerprints in production — only the
     * desktop does — but the cross-runtime parity guarantee in the
     * test vectors README requires a PHP runner so the spec invariant
     * (HKDF info + HMAC-SHA256 + base64 wire encoding) holds on both
     * sides byte-exact.
     */
    #[DataProvider("contentFingerprintCases")]
    public function test_content_fingerprint_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];
        $masterKey = hex2bin($inputs['vault_master_key']);
        $plaintextSha256 = hex2bin($inputs['plaintext_sha256']);

        $subkey = VaultCrypto::deriveContentFingerprintKey($masterKey);
        $fingerprint = VaultCrypto::makeContentFingerprint($subkey, $plaintextSha256);

        self::assertSame($expected['subkey'], bin2hex($subkey), "{$name}: subkey mismatch");
        self::assertSame(
            $expected['fingerprint_b64'], $fingerprint,
            "{$name}: fingerprint mismatch — PHP/Python parity break"
        );
    }

    // ---------------------------------------------------------------- export bundle

    public static function exportCases(): array { return self::loadCases('export_bundle_v1.json'); }

    #[DataProvider("exportCases")]
    public function test_export_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];

        $salt = hex2bin($inputs['argon_salt']);
        $outerNonce = hex2bin($inputs['outer_nonce']);
        $exportFileKey = hex2bin($inputs['export_file_key']);
        $memKib = (int)$inputs['argon_memory_kib'];
        $iters = (int)$inputs['argon_iterations'];

        $outerHeader = VaultCrypto::buildExportOuterHeader(
            $memKib, $iters, (int)$inputs['argon_parallelism'], $salt, $outerNonce
        );
        $wrapKey = VaultCrypto::deriveExportWrapKey($inputs['passphrase'], $salt, $memKib, $iters);
        $wrapAad = VaultCrypto::buildExportWrapAad($inputs['vault_id']);
        $wrappedKeyEnvelope = VaultCrypto::aeadEncrypt($exportFileKey, $wrapKey, $outerNonce, $wrapAad);

        if (isset($expected['expected_error'])) {
            $decryptWrapKey = $wrapKey;
            $decryptEnvelope = $wrappedKeyEnvelope;
            $decryptOuter = $outerHeader;
            if (isset($inputs['decrypt_passphrase_override'])) {
                $decryptWrapKey = VaultCrypto::deriveExportWrapKey(
                    $inputs['decrypt_passphrase_override'], $salt, $memKib, $iters
                );
            }
            $tamper = $case['tamper'] ?? [];
            if (isset($tamper['wrapped_key_byte_xor'])) {
                $spec = $tamper['wrapped_key_byte_xor'];
                $decryptEnvelope[$spec['offset']] = chr(
                    ord($decryptEnvelope[$spec['offset']]) ^ hexdec($spec['xor'])
                );
            }
            if (isset($tamper['envelope_byte_xor'])) {
                // Format-version tamper targets the outer header (after
                // the 4-byte ``DCVE`` magic, byte 4 is format_version).
                $spec = $tamper['envelope_byte_xor'];
                $decryptOuter[$spec['offset']] = chr(
                    ord($decryptOuter[$spec['offset']]) ^ hexdec($spec['xor'])
                );
            }
            if ($expected['expected_error'] === 'vault_format_version_unsupported') {
                $this->expectException(VaultFormatVersionUnsupportedError::class);
                VaultCrypto::assertSupportedFormatVersion($decryptOuter, 'export_outer', 4);
                return;
            }
            $this->expectException(SodiumException::class);
            VaultCrypto::aeadDecrypt($decryptEnvelope, $decryptWrapKey, $outerNonce, $wrapAad);
            return;
        }

        self::assertSame($expected['outer_header_bytes'], bin2hex($outerHeader));
        self::assertSame($expected['wrap_key'], bin2hex($wrapKey));
        self::assertSame($expected['wrap_aad'], bin2hex($wrapAad));
        self::assertSame($expected['wrapped_key_envelope'], bin2hex($wrappedKeyEnvelope));
        self::assertSame($exportFileKey, VaultCrypto::aeadDecrypt($wrappedKeyEnvelope, $wrapKey, $outerNonce, $wrapAad));
    }
}
