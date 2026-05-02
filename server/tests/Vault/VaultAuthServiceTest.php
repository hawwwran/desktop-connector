<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Unit + integration tests for VaultAuthService::requireVaultAuth (T1.5).
 *
 * Acceptance:
 *   - Middleware-only test verifies 401 vault_auth_failed on missing /
 *     invalid header.
 *   - Integration test with stub controller verifies device + vault
 *     auth combine.
 *
 * Tests manipulate $_SERVER directly because that's how the Auth
 * services read the headers — same approach as production. setUp /
 * tearDown clear and reset to keep tests independent.
 */
final class VaultAuthServiceTest extends TestCase
{
    private string $dbPath;
    private Database $db;
    private VaultsRepository $vaultsRepo;

    private const VAULT_ID    = 'H9K7M4Q2Z8TD';
    private const VAULT_SECRET = 'super-high-entropy-vault-access-secret-32bytes';
    private const HEADER_HASH = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const MFST_HASH   = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const NOW         = 1714680000;

    private const DEVICE_ID    = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';   // 32 hex
    private const DEVICE_TOKEN = 'device-bearer-token';

    private array $serverBackup = [];

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_auth_test_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();

        $this->vaultsRepo = new VaultsRepository($this->db);
        $this->vaultsRepo->create(
            self::VAULT_ID,
            hash('sha256', self::VAULT_SECRET, true),
            "\xde\xad",
            self::HEADER_HASH,
            self::MFST_HASH,
            self::NOW
        );

        // Seed a registered device so AuthService::requireAuth can match.
        $devices = new DeviceRepository($this->db);
        $devices->insertDevice(
            self::DEVICE_ID,
            base64_encode(random_bytes(32)),
            self::DEVICE_TOKEN,
            'desktop',
            self::NOW
        );

        // Snapshot $_SERVER keys we touch so tearDown can restore.
        foreach (['HTTP_X_DEVICE_ID', 'HTTP_AUTHORIZATION', 'HTTP_X_VAULT_ID', 'HTTP_X_VAULT_AUTHORIZATION', 'REQUEST_URI'] as $k) {
            $this->serverBackup[$k] = $_SERVER[$k] ?? null;
            unset($_SERVER[$k]);
        }
        $_SERVER['REQUEST_URI'] = '/api/vaults/' . self::VAULT_ID . '/header';
    }

    protected function tearDown(): void
    {
        foreach ($this->serverBackup as $k => $v) {
            if ($v === null) {
                unset($_SERVER[$k]);
            } else {
                $_SERVER[$k] = $v;
            }
        }
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
    }

    private function setValidDeviceAuth(): void
    {
        $_SERVER['HTTP_X_DEVICE_ID']   = self::DEVICE_ID;
        $_SERVER['HTTP_AUTHORIZATION'] = 'Bearer ' . self::DEVICE_TOKEN;
    }

    private function setValidVaultAuth(): void
    {
        $_SERVER['HTTP_X_VAULT_ID']            = self::VAULT_ID;
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;
    }

    // ------------------------------------------------- device-auth failures

    public function test_throws_device_kind_when_no_auth_headers(): void
    {
        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultAuthFailedError');
        } catch (VaultAuthFailedError $e) {
            self::assertSame(401, $e->status);
            self::assertSame('vault_auth_failed', $e->errorCode);
            self::assertSame(['kind' => 'device'], $e->details);
        }
    }

    public function test_throws_device_kind_when_token_wrong(): void
    {
        $_SERVER['HTTP_X_DEVICE_ID']   = self::DEVICE_ID;
        $_SERVER['HTTP_AUTHORIZATION'] = 'Bearer wrong-token';
        $this->setValidVaultAuth();

        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultAuthFailedError');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('device', $e->details['kind']);
        }
    }

    public function test_throws_device_kind_when_device_id_unknown(): void
    {
        $_SERVER['HTTP_X_DEVICE_ID']   = 'ffffffffffffffffffffffffffffffff';
        $_SERVER['HTTP_AUTHORIZATION'] = 'Bearer ' . self::DEVICE_TOKEN;
        $this->setValidVaultAuth();

        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultAuthFailedError');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('device', $e->details['kind']);
        }
    }

    // ------------------------------------------------- vault-auth failures

    public function test_throws_vault_kind_when_vault_header_missing(): void
    {
        $this->setValidDeviceAuth();
        // No X-Vault-Authorization at all.

        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultAuthFailedError');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('vault', $e->details['kind']);
        }
    }

    public function test_throws_vault_kind_when_bearer_prefix_missing(): void
    {
        $this->setValidDeviceAuth();
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = self::VAULT_SECRET;   // no "Bearer "

        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultAuthFailedError');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('vault', $e->details['kind']);
        }
    }

    public function test_throws_vault_kind_when_secret_empty(): void
    {
        $this->setValidDeviceAuth();
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ';   // bearer with no body

        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultAuthFailedError');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('vault', $e->details['kind']);
        }
    }

    public function test_throws_vault_kind_when_secret_wrong(): void
    {
        $this->setValidDeviceAuth();
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer not-the-real-secret';

        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultAuthFailedError');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('vault', $e->details['kind']);
        }
    }

    // ------------------------------------------------- not-found / mismatch

    public function test_throws_vault_not_found_for_unknown_path_id(): void
    {
        $this->setValidDeviceAuth();
        // X-Vault-ID intentionally omitted so the mismatch guard doesn't
        // fire ahead of the not-found check. Per §2 the header is
        // redundant; controllers that only see the path id should still
        // surface vault_not_found cleanly.
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;

        try {
            VaultAuthService::requireVaultAuth($this->db, 'UNKNOWN00000');
            self::fail('expected VaultNotFoundError');
        } catch (VaultNotFoundError $e) {
            self::assertSame(404, $e->status);
            self::assertSame('vault_not_found', $e->errorCode);
            self::assertSame('UNKNOWN00000', $e->details['vault_id']);
        }
    }

    public function test_throws_invalid_request_when_x_vault_id_mismatches_path(): void
    {
        $this->setValidDeviceAuth();
        $_SERVER['HTTP_X_VAULT_ID']            = 'OTHERVAULT00';
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;

        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultInvalidRequestError');
        } catch (VaultInvalidRequestError $e) {
            self::assertSame(400, $e->status);
            self::assertSame('vault_invalid_request', $e->errorCode);
            self::assertSame('vault_id', $e->details['field']);
        }
    }

    // ------------------------------------------------- success

    public function test_returns_vault_row_on_success(): void
    {
        $this->setValidDeviceAuth();
        $this->setValidVaultAuth();

        $vault = VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
        self::assertSame(self::VAULT_ID, $vault['vault_id']);
        self::assertSame(self::HEADER_HASH, $vault['header_hash']);
    }

    public function test_omitted_x_vault_id_header_is_acceptable(): void
    {
        // Per vault-v1.md §2 the X-Vault-ID header *redundantly* mirrors
        // the path. Omitting it is fine; only mismatch is rejected.
        $this->setValidDeviceAuth();
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;

        $vault = VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
        self::assertSame(self::VAULT_ID, $vault['vault_id']);
    }

    // ------------------------------------------------- integration: stub controller

    public function test_stub_controller_combines_device_and_vault_auth(): void
    {
        // Mimics what a vault endpoint will do at T1.6: call requireVaultAuth
        // and proceed on success; surface the right error envelope on
        // failure. The "controller" here is a closure for testability.
        $stubController = function (Database $db, string $vaultId): array {
            $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
            return [
                'ok' => true,
                'data' => [
                    'vault_id' => $vault['vault_id'],
                    'header_revision' => (int)$vault['header_revision'],
                ],
            ];
        };

        // Both auths valid → controller proceeds and returns its data.
        $this->setValidDeviceAuth();
        $this->setValidVaultAuth();
        $resp = $stubController($this->db, self::VAULT_ID);
        self::assertTrue($resp['ok']);
        self::assertSame(self::VAULT_ID, $resp['data']['vault_id']);

        // Device auth missing → controller never runs; error is device-kind.
        unset($_SERVER['HTTP_X_DEVICE_ID'], $_SERVER['HTTP_AUTHORIZATION']);
        try {
            $stubController($this->db, self::VAULT_ID);
            self::fail('expected device-kind auth failure to bubble out');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('device', $e->details['kind']);
        }

        // Device auth valid but vault auth wrong → vault-kind failure.
        $this->setValidDeviceAuth();
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer wrong';
        try {
            $stubController($this->db, self::VAULT_ID);
            self::fail('expected vault-kind auth failure to bubble out');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('vault', $e->details['kind']);
        }
    }

    // ------------------------------------------------- create-path helper

    public function test_requireDeviceAuthForCreate_returns_device_id(): void
    {
        $this->setValidDeviceAuth();
        $deviceId = VaultAuthService::requireDeviceAuthForCreate($this->db);
        self::assertSame(self::DEVICE_ID, $deviceId);
    }

    public function test_requireDeviceAuthForCreate_throws_device_kind_when_missing(): void
    {
        try {
            VaultAuthService::requireDeviceAuthForCreate($this->db);
            self::fail('expected VaultAuthFailedError');
        } catch (VaultAuthFailedError $e) {
            self::assertSame('device', $e->details['kind']);
        }
    }
}
