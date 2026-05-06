<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Polish-pass unit tests for AAD-build invariants enforced at write
 * time so they line up with the read-time checks in
 * ``parseManifestEnvelopeHeader``. F-S17 — ``buildManifestAad`` and
 * ``buildDeviceGrantAad`` both reject anything other than 32 lowercase
 * hex chars for the device id, instead of just length-checking.
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

    public function test_buildManifestAad_accepts_lowercase_hex_device_id(): void
    {
        $aad = VaultCrypto::buildManifestAad(self::VAULT_ID, 1, 0, self::DEVICE_ID_LOWER);
        self::assertNotEmpty($aad);
    }

    public function test_buildManifestAad_rejects_uppercase_hex_device_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessage('lowercase hex');
        VaultCrypto::buildManifestAad(self::VAULT_ID, 1, 0, self::DEVICE_ID_UPPER);
    }

    public function test_buildManifestAad_rejects_short_device_id(): void
    {
        $this->expectException(InvalidArgumentException::class);
        VaultCrypto::buildManifestAad(self::VAULT_ID, 1, 0, 'a1b2c3d4');
    }

    public function test_buildManifestAad_rejects_non_hex_device_id(): void
    {
        // 32-char string but contains non-hex `g` and `z`.
        $this->expectException(InvalidArgumentException::class);
        VaultCrypto::buildManifestAad(self::VAULT_ID, 1, 0, str_repeat('gz', 16));
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
}
