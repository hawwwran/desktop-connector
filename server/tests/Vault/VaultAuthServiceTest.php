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

    /**
     * Review §1.H1: protocol §10 caps vault auth at 10 attempts per
     * (device, vault) per minute. Each successful auth still bills
     * the counter — the limit caps total attempts, successful or
     * not, so a misbehaving client storm can't drown out the IDS
     * signal. Hitting the cap must surface as a 429 with
     * ``Retry-After``.
     */
    public function test_vault_auth_429s_after_attempts_cap(): void
    {
        $this->setValidDeviceAuth();
        $this->setValidVaultAuth();

        // Burn the 10 allowed attempts.
        for ($i = 0; $i < 10; $i++) {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
        }
        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultRateLimitedError on the 11th attempt');
        } catch (VaultRateLimitedError $e) {
            self::assertSame(429, $e->status);
            self::assertSame('vault_rate_limited', $e->errorCode);
            self::assertArrayHasKey('retry_after_ms', $e->details);
            self::assertGreaterThan(0, $e->details['retry_after_ms']);
            // Retry-After header populated.
            self::assertArrayHasKey('Retry-After', $e->headers);
        }
    }

    /**
     * Review §1.H1: the counter is per-(device, vault). Two
     * different vaults must not share a budget — otherwise a
     * legitimate user with two unlocks would lock themselves out
     * trying to fix a typo.
     */
    public function test_vault_auth_counter_is_per_vault(): void
    {
        $otherVaultId = 'P3QR4ST5UVWY';
        $otherSecret = 'other-vault-secret-32-bytes-long-padding-padding';
        $this->vaultsRepo->create(
            $otherVaultId,
            hash('sha256', $otherSecret, true),
            "\xde\xad", self::HEADER_HASH, self::MFST_HASH, self::NOW,
        );
        $this->setValidDeviceAuth();
        // Burn 10 against VAULT_ID.
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;
        for ($i = 0; $i < 10; $i++) {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
        }
        // Switch to the other vault — should succeed (fresh counter).
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . $otherSecret;
        $vault = VaultAuthService::requireVaultAuth($this->db, $otherVaultId);
        self::assertSame($otherVaultId, $vault['vault_id']);
    }

    /**
     * Review §1.H1: a failed-auth attempt also bills the counter, so
     * a password-guessing storm hits the cap on attempt 11 even when
     * every prior attempt was a 401. (Auth-kind failure is the
     * primary IDS-relevant case the spec rate-limits.)
     */
    public function test_failed_vault_auth_still_bills_counter(): void
    {
        $this->setValidDeviceAuth();
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer wrong-secret';
        // 10 failed attempts all return vault_auth_failed.
        for ($i = 0; $i < 10; $i++) {
            try {
                VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            } catch (VaultAuthFailedError $e) {
                $this->assertSame('vault', $e->details['kind']);
            }
        }
        // 11th attempt: the rate limit fires BEFORE the auth check
        // so the user sees a 429 (telemetry / IDS) rather than yet
        // another 401.
        try {
            VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            self::fail('expected VaultRateLimitedError');
        } catch (VaultRateLimitedError $e) {
            self::assertSame(429, $e->status);
        }
    }

    /**
     * B5 SO-2: ``vaultAuthLimit`` config raises the cap above the
     * hardcoded floor of 10. Verifies that with the config set to
     * 15, attempts 11..15 still succeed and the 16th is the one
     * that 429s.
     */
    public function test_configured_vault_auth_limit_raises_cap_above_floor(): void
    {
        $configPath = __DIR__ . '/../../data/config.json';
        $backup = is_file($configPath) ? file_get_contents($configPath) : null;
        try {
            file_put_contents($configPath, json_encode(['vaultAuthLimit' => 15]));
            Config::flush();
            self::assertSame(15, Config::vaultAuthLimit());

            $this->setValidDeviceAuth();
            $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;
            for ($i = 0; $i < 15; $i++) {
                $vault = VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
                self::assertSame(self::VAULT_ID, $vault['vault_id']);
            }
            try {
                VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
                self::fail('expected 429 on attempt 16 when limit=15');
            } catch (VaultRateLimitedError $e) {
                self::assertSame(429, $e->status);
            }
        } finally {
            if ($backup === null) {
                @unlink($configPath);
            } else {
                file_put_contents($configPath, $backup);
            }
            Config::flush();
        }
    }

    /**
     * B5 SO-2: the floor protects the original §1.H1 design. Even if
     * an operator types ``vaultAuthLimit: 2`` (intentionally or via a
     * typo), Config::vaultAuthLimit() clamps back to 10, and the
     * rate-limit fires on attempt 11 (not 3).
     */
    public function test_vault_auth_limit_below_floor_clamped_to_ten(): void
    {
        $configPath = __DIR__ . '/../../data/config.json';
        $backup = is_file($configPath) ? file_get_contents($configPath) : null;
        try {
            file_put_contents($configPath, json_encode(['vaultAuthLimit' => 2]));
            Config::flush();
            self::assertSame(10, Config::vaultAuthLimit(),
                'configured value below floor must be clamped to VAULT_AUTH_LIMIT_FLOOR');

            $this->setValidDeviceAuth();
            $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;
            for ($i = 0; $i < 10; $i++) {
                VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
            }
            try {
                VaultAuthService::requireVaultAuth($this->db, self::VAULT_ID);
                self::fail('expected 429 on attempt 11 — floor must hold even with config=2');
            } catch (VaultRateLimitedError $e) {
                self::assertSame(429, $e->status);
            }
        } finally {
            if ($backup === null) {
                @unlink($configPath);
            } else {
                file_put_contents($configPath, $backup);
            }
            Config::flush();
        }
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

    /**
     * Review §1.H1: protocol §10 caps create-vault at 5 per device
     * per hour. The 6th attempt within the hour gets 429.
     */
    public function test_create_vault_429s_after_attempts_cap(): void
    {
        $this->setValidDeviceAuth();
        for ($i = 0; $i < 5; $i++) {
            VaultAuthService::requireDeviceAuthForCreate($this->db);
        }
        try {
            VaultAuthService::requireDeviceAuthForCreate($this->db);
            self::fail('expected VaultRateLimitedError on the 6th attempt');
        } catch (VaultRateLimitedError $e) {
            self::assertSame(429, $e->status);
            self::assertSame('vault_rate_limited', $e->errorCode);
            self::assertGreaterThan(0, $e->details['retry_after_ms']);
        }
    }

    // ------------------------------------------------- F-T12: requireRole

    /**
     * F-T12: pin the §D11 role rank matrix directly. Pre-fix
     * coverage for ``requireRole`` was indirect through every
     * controller test that happened to invoke a role-gated endpoint —
     * a future regression that scrambles the rank table or removes
     * the revoked-grant check would have been silent until a
     * controller test happened to trip it. The matrix below tests
     * each (granted, required) pair representatively.
     */
    private function seedGrant(
        string $deviceId,
        string $role,
        ?int $revokedAt = null
    ): void {
        $grants = new VaultDeviceGrantsRepository($this->db);
        $grants->insertGrant(
            'gr_v1_' . str_pad((string)random_int(0, PHP_INT_MAX), 24, 'a', STR_PAD_LEFT),
            self::VAULT_ID,
            $deviceId,
            'name',
            $role,
            self::DEVICE_ID,   // granted_by
            'qr',
            self::NOW
        );
        if ($revokedAt !== null) {
            $grants->revoke(self::VAULT_ID, $deviceId, self::DEVICE_ID, $revokedAt);
        }
    }

    public function test_requireRole_admin_passes_every_gate(): void
    {
        $this->seedGrant(self::DEVICE_ID, 'admin');
        foreach (['read-only', 'browse-upload', 'sync', 'admin'] as $gate) {
            $row = VaultAuthService::requireRole(
                $this->db, self::VAULT_ID, self::DEVICE_ID, $gate
            );
            self::assertSame('admin', $row['role'], "admin failed gate {$gate}");
        }
    }

    public function test_requireRole_each_role_passes_own_gate(): void
    {
        $cases = [
            'b1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6' => 'read-only',
            'b2b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6' => 'browse-upload',
            'b3b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6' => 'sync',
        ];
        foreach ($cases as $deviceId => $role) {
            $this->seedGrant($deviceId, $role);
            $row = VaultAuthService::requireRole(
                $this->db, self::VAULT_ID, $deviceId, $role
            );
            self::assertSame($role, $row['role']);
        }
    }

    public function test_requireRole_lower_rank_rejected_by_higher_gate(): void
    {
        // read-only granted, browse-upload required → 403
        $a = 'b4b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
        $this->seedGrant($a, 'read-only');
        try {
            VaultAuthService::requireRole(
                $this->db, self::VAULT_ID, $a, 'browse-upload'
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame(403, $e->status);
            self::assertSame('browse-upload', $e->details['required_role']);
        }

        // browse-upload granted, sync required → 403
        $b = 'b5b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
        $this->seedGrant($b, 'browse-upload');
        try {
            VaultAuthService::requireRole(
                $this->db, self::VAULT_ID, $b, 'sync'
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame('sync', $e->details['required_role']);
        }

        // sync granted, admin required → 403
        $c = 'b6b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
        $this->seedGrant($c, 'sync');
        try {
            VaultAuthService::requireRole(
                $this->db, self::VAULT_ID, $c, 'admin'
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame('admin', $e->details['required_role']);
        }
    }

    public function test_requireRole_rejects_revoked_grant(): void
    {
        $deviceId = 'b7b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
        $this->seedGrant($deviceId, 'admin', revokedAt: self::NOW + 60);
        try {
            VaultAuthService::requireRole(
                $this->db, self::VAULT_ID, $deviceId, 'read-only'
            );
            self::fail('revoked grant must not pass even the lowest gate');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame(403, $e->status);
            self::assertSame('read-only', $e->details['required_role']);
            // The exception message names the revocation so the operator
            // can tell apart "no grant" from "revoked grant".
            self::assertStringContainsString('revoked', $e->getMessage());
        }
    }

    public function test_requireRole_rejects_unknown_device(): void
    {
        // Unknown device id has no grant row.
        try {
            VaultAuthService::requireRole(
                $this->db, self::VAULT_ID,
                'b8b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
                'read-only'
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame(403, $e->status);
            self::assertStringContainsString('not a granted device', $e->getMessage());
        }
    }

    public function test_requireRole_unknown_role_throws_invalid_argument(): void
    {
        // Caller bug — an unknown role string should fail loudly, not
        // silently grant or reject.
        $this->seedGrant(self::DEVICE_ID, 'admin');
        $this->expectException(InvalidArgumentException::class);
        VaultAuthService::requireRole(
            $this->db, self::VAULT_ID, self::DEVICE_ID, 'superuser'
        );
    }

    public function test_requireRole_bumps_last_seen_on_success(): void
    {
        $deviceId = 'b9b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
        $this->seedGrant($deviceId, 'sync');
        $before = (new VaultDeviceGrantsRepository($this->db))
            ->getByDevice(self::VAULT_ID, $deviceId);
        self::assertNull($before['last_seen_at']);

        VaultAuthService::requireRole(
            $this->db, self::VAULT_ID, $deviceId, 'sync'
        );

        $after = (new VaultDeviceGrantsRepository($this->db))
            ->getByDevice(self::VAULT_ID, $deviceId);
        self::assertNotNull($after['last_seen_at']);
        self::assertGreaterThan(0, (int)$after['last_seen_at']);
    }
}
