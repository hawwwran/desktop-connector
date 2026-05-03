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

    // ---------------------------------------------------------------- manifest

    public static function manifestCases(): array
    {
        return self::loadCases('manifest_v1.json');
    }

    #[DataProvider("manifestCases")]
    public function test_manifest_case(string $name, array $case): void
    {
        $inputs = $case['inputs'];
        $expected = $case['expected'];

        $masterKey = hex2bin($inputs['vault_master_key']);
        $nonce = hex2bin($inputs['nonce']);
        $plaintext = base64_decode($inputs['manifest_plaintext'], true);

        $aad = VaultCrypto::buildManifestAad(
            $inputs['vault_id'],
            (int)$inputs['revision'],
            (int)$inputs['parent_revision'],
            $inputs['author_device_id']
        );
        $subkey = VaultCrypto::deriveSubkey('dc-vault-v1/manifest', $masterKey);
        $ct = VaultCrypto::aeadEncrypt($plaintext, $subkey, $nonce, $aad);
        $envelope = VaultCrypto::buildManifestEnvelope(
            $inputs['vault_id'],
            (int)$inputs['revision'],
            (int)$inputs['parent_revision'],
            $inputs['author_device_id'],
            $nonce,
            $ct
        );

        if (isset($expected['expected_error'])) {
            $tamper = $case['tamper'] ?? [];
            $decryptAad = $aad;
            $decryptCt = $ct;
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $buf = $envelope;
                $buf[$spec['offset']] = chr(ord($buf[$spec['offset']]) ^ hexdec($spec['xor']));
                // Manifest envelope plaintext header already includes the
                // nonce; byte 85 is the AEAD ciphertext start.
                $decryptCt = substr($buf, 85);
            }
            if (isset($tamper['aad_override'])) {
                $decryptAad = hex2bin($tamper['aad_override']);
            }
            $this->expectException(SodiumException::class);
            VaultCrypto::aeadDecrypt($decryptCt, $subkey, $nonce, $decryptAad);
            return;
        }

        self::assertSame($expected['aad'], bin2hex($aad), "{$name}: AAD mismatch");
        self::assertSame($expected['subkey'], bin2hex($subkey), "{$name}: subkey mismatch");
        self::assertSame($expected['aead_ciphertext_and_tag'], bin2hex($ct), "{$name}: ciphertext mismatch");
        self::assertSame($expected['envelope_bytes'], bin2hex($envelope), "{$name}: envelope mismatch");
        self::assertSame($plaintext, VaultCrypto::aeadDecrypt($ct, $subkey, $nonce, $aad), "{$name}: round-trip");
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
            $decryptCt = $ct;
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $buf = $envelope;
                $buf[$spec['offset']] = chr(ord($buf[$spec['offset']]) ^ hexdec($spec['xor']));
                // Header envelope: 1+12+8 = 21 bytes plaintext header, +24 nonce.
                $decryptCt = substr($buf, 21 + 24);
            }
            if (isset($tamper['aad_override'])) {
                $decryptAad = hex2bin($tamper['aad_override']);
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
                // Recovery envelope plaintext header = 1+12+30+16+24 = 83 bytes.
                $decryptCt = substr($buf, 83);
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
            $decryptCt = $ct;
            if (isset($tamper['envelope_byte_xor'])) {
                $spec = $tamper['envelope_byte_xor'];
                $buf = $envelope;
                $buf[$spec['offset']] = chr(ord($buf[$spec['offset']]) ^ hexdec($spec['xor']));
                // Device grant: 1+12+30+32 = 75 bytes header + 24 nonce.
                $decryptCt = substr($buf, 75 + 24);
            }
            if (isset($tamper['aad_override'])) {
                $decryptAad = hex2bin($tamper['aad_override']);
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
