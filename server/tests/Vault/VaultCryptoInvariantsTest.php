<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Polish-pass unit tests for AAD-build invariants enforced at write
 * time so they line up with the read-time checks in
 * ``parseRootEnvelopeHeader`` / ``parseShardEnvelopeHeader``. F-S17 —
 * ``buildRootAad`` / ``buildShardAad`` / ``buildDeviceGrantAad`` all
 * reject anything other than 32 lowercase hex chars for the device id,
 * instead of just length-checking.
 *
 * Without these checks a caller that computed AAD with an upper-case
 * device id would derive a different AAD than the parser reconstructs
 * on read, and the relay-side AEAD verification would fail with no
 * useful 400 (the desktop sees the failure first).
 */
final class VaultCryptoInvariantsTest extends TestCase
{
    private const VAULT_ID         = 'ABCD2345WXYZ';
    private const DEVICE_ID_LOWER  = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
    private const DEVICE_ID_UPPER  = 'A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6';
    private const GRANT_ID_30B     = 'gr_v1_aaaaaaaaaaaaaaaaaaaaaaaa';

    public function test_buildRootAad_accepts_lowercase_hex_device_id(): void
    {
        $aad = VaultCrypto::buildRootAad(self::VAULT_ID, 1, 0, self::DEVICE_ID_LOWER);
        self::assertNotEmpty($aad);
    }

    public function test_buildRootAad_rejects_uppercase_hex_device_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessage('lowercase hex');
        VaultCrypto::buildRootAad(self::VAULT_ID, 1, 0, self::DEVICE_ID_UPPER);
    }

    public function test_buildRootAad_rejects_short_device_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        VaultCrypto::buildRootAad(self::VAULT_ID, 1, 0, 'a1b2c3d4');
    }

    public function test_buildRootAad_rejects_non_hex_device_id(): void
    {
        // 32-char string but contains non-hex `g` and `z`.
        $this->expectException(InvalidArgumentException::class);
        VaultCrypto::buildRootAad(self::VAULT_ID, 1, 0, str_repeat('gz', 16));
    }

    public function test_buildDeviceGrantAad_accepts_lowercase_hex_claimant_id(): void
    {
        self::assertSame(strlen(self::GRANT_ID_30B), 30);
        $aad = VaultCrypto::buildDeviceGrantAad(
            self::VAULT_ID, self::GRANT_ID_30B, self::DEVICE_ID_LOWER
        );
        self::assertNotEmpty($aad);
    }

    public function test_buildDeviceGrantAad_rejects_uppercase_hex_claimant_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessage('lowercase hex');
        VaultCrypto::buildDeviceGrantAad(
            self::VAULT_ID, self::GRANT_ID_30B, self::DEVICE_ID_UPPER
        );
    }

    public function test_buildDeviceGrantAad_rejects_non_hex_claimant_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        VaultCrypto::buildDeviceGrantAad(
            self::VAULT_ID, self::GRANT_ID_30B, str_repeat('xy', 16)
        );
    }

    /**
     * Review §1.M2 — every AAD builder MUST canonicalize and assert the
     * 12-byte vault_id length. Pre-fix buildChunkAad / buildHeaderAad /
     * buildRecoveryAad / buildDeviceGrantAad accepted whatever
     * ``normalizeVaultId`` returned (which could be empty if the input
     * was malformed before reaching the builder). A future controller
     * that forgot ``normalizeVaultId`` would have silently produced a
     * wrong-length AAD that AEAD-passes locally but diverges from the
     * Python twin's output, breaking the cross-runtime parity gate.
     */
    public function test_buildChunkAad_rejects_malformed_vault_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessage('12 bytes');
        VaultCrypto::buildChunkAad(
            'TOO-SHORT', self::GRANT_ID_30B,
            str_repeat('a', 30), str_repeat('b', 30), 0, 0,
        );
    }

    public function test_buildHeaderAad_rejects_malformed_vault_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessage('12 bytes');
        VaultCrypto::buildHeaderAad('TOO-SHORT', 1);
    }

    public function test_buildRecoveryAad_rejects_malformed_vault_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessage('12 bytes');
        VaultCrypto::buildRecoveryAad('TOO-SHORT', self::GRANT_ID_30B);
    }

    public function test_buildDeviceGrantAad_rejects_malformed_vault_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessage('12 bytes');
        VaultCrypto::buildDeviceGrantAad(
            'TOO-SHORT', self::GRANT_ID_30B, self::DEVICE_ID_LOWER,
        );
    }

    /**
     * Review §2.M5 — passing a non-ASCII passphrase through
     * argon2idKdf when the ``intl`` extension is missing MUST hard-
     * fail rather than silently diverge from the Python twin (which
     * always NFC-normalizes). On hosts WITH ``intl`` the
     * normalization runs and the call succeeds; on hosts WITHOUT,
     * the new guard raises with a clear remediation message.
     *
     * Pure ASCII passphrases work in both worlds (ASCII == NFC(ASCII)).
     */
    public function test_argon2idKdf_rejects_non_ascii_passphrase_when_intl_missing(): void
    {
        if (class_exists('Normalizer')) {
            self::markTestSkipped('host has php-intl; the hard-fail path is unreachable');
        }
        $salt = str_repeat("\x00", 16);
        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('php-intl');
        VaultCrypto::argon2idKdf(
            "caf\u{00e9}",  // U+00E9 = "é" — non-ASCII (double quotes interpret \u)
            $salt, 32, 8192, 2,
        );
    }

    public function test_argon2idKdf_accepts_ascii_passphrase_without_intl(): void
    {
        // Pure ASCII = its own NFC form; safe passthrough.
        $salt = str_repeat("\x00", 16);
        $out = VaultCrypto::argon2idKdf('correct horse battery staple', $salt, 32, 8192, 2);
        self::assertSame(32, strlen($out));
    }
}
