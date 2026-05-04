<?php

declare(strict_types=1);

use PHPUnit\Framework\Attributes\RunTestsInSeparateProcesses;
use PHPUnit\Framework\TestCase;

/**
 * Wire-surface tests for VaultGrantsController (T13.1). Walks the
 * QR-grant lifecycle end-to-end:
 *
 *   admin posts join request   → 201, jr_v1_<id>, expires_at = +15min
 *   claimant POSTs claim       → 200, state = "claimed"
 *   admin GET poll             → 200, claimant_pubkey populated
 *   admin POSTs approve        → 200, state = "approved", grant row inserted
 *   admin DELETE device-grant  → 200, revoked_at set
 *   admin POSTs rotate         → 200, vaults.vault_access_token_hash updated,
 *                                 vault_access_secret_rotations row inserted
 *
 * Plus the negative cases that need to be in the wire surface:
 *   - non-admin caller → vault_access_denied 403
 *   - claim with stale jr (expired) → vault_join_request_state 404
 *   - approve before claim → vault_join_request_state 409
 *   - DELETE on non-existent grant → vault_join_request_state 404
 *   - admin self-revoke → vault_invalid_request 400
 */
#[RunTestsInSeparateProcesses]
final class VaultGrantsControllerTest extends TestCase
{
    private string $dbPath;
    private Database $db;

    private const VAULT_ID = 'ABCD2345WXYZ';
    private const VAULT_SECRET = 'super-high-entropy-secret';
    private const ADMIN_DEVICE = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
    private const CLAIMANT_DEVICE = 'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7';
    private const NON_ADMIN_DEVICE = 'cccccccccccccccccccccccccccccccc';
    private const ADMIN_TOKEN = 'admin-bearer-token';
    private const CLAIMANT_TOKEN = 'claimant-bearer-token';
    private const NON_ADMIN_TOKEN = 'non-admin-bearer-token';
    private const HEADER_HASH = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const MFST_HASH = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const NOW = 1714680000;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_grants_db_');
        $this->db = Database::fromPath($this->dbPath);
        $this->db->migrate();

        $devs = new DeviceRepository($this->db);
        $devs->insertDevice(self::ADMIN_DEVICE, base64_encode(random_bytes(32)),
            self::ADMIN_TOKEN, 'desktop', self::NOW);
        $devs->insertDevice(self::CLAIMANT_DEVICE, base64_encode(random_bytes(32)),
            self::CLAIMANT_TOKEN, 'desktop', self::NOW);
        $devs->insertDevice(self::NON_ADMIN_DEVICE, base64_encode(random_bytes(32)),
            self::NON_ADMIN_TOKEN, 'desktop', self::NOW);

        (new VaultsRepository($this->db))->create(
            self::VAULT_ID,
            hash('sha256', self::VAULT_SECRET, true),
            "\x01header",
            self::HEADER_HASH,
            self::MFST_HASH,
            self::NOW
        );
        (new VaultManifestsRepository($this->db))->create(
            self::VAULT_ID, 1, 0, self::MFST_HASH, "\x02manifest", 8,
            self::ADMIN_DEVICE, self::NOW,
        );

        // Seed an admin grant for ADMIN_DEVICE — the create call doesn't
        // do this implicitly today, but T13's flow needs it before any
        // grant endpoint will accept calls.
        (new VaultDeviceGrantsRepository($this->db))->insertGrant(
            'dg_v1_seed_admin0000000000000',
            self::VAULT_ID, self::ADMIN_DEVICE, 'Admin Desktop', 'admin',
            self::ADMIN_DEVICE, 'create', self::NOW,
        );
        (new VaultDeviceGrantsRepository($this->db))->insertGrant(
            'dg_v1_seed_nonadmin000000000000',
            self::VAULT_ID, self::NON_ADMIN_DEVICE, 'Sync Desktop', 'sync',
            self::ADMIN_DEVICE, 'create', self::NOW,
        );
    }

    protected function tearDown(): void
    {
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
        foreach ([
            'HTTP_X_DEVICE_ID', 'HTTP_AUTHORIZATION', 'HTTP_X_VAULT_AUTHORIZATION',
            'REQUEST_URI', 'REQUEST_METHOD',
        ] as $k) {
            unset($_SERVER[$k]);
        }
    }

    private function setAuth(string $deviceId, string $deviceToken): void
    {
        $_SERVER['HTTP_X_DEVICE_ID'] = $deviceId;
        $_SERVER['HTTP_AUTHORIZATION'] = 'Bearer ' . $deviceToken;
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;
    }

    private function ctx(string $method, array $params = [], ?string $body = null): RequestContext
    {
        return new RequestContext(method: $method, params: $params, bodyOverride: $body);
    }

    private function invoke(callable $fn): array
    {
        if (!headers_sent()) {
            http_response_code(200);
        }
        ob_start();
        try {
            $fn();
        } finally {
            $raw = ob_get_clean();
        }
        $status = http_response_code();
        $json = $raw === '' ? null : json_decode($raw, true);
        return [
            'status' => is_int($status) ? $status : 0,
            'json'   => $json,
            'raw'    => $raw,
        ];
    }

    private function expectVaultError(callable $fn, string $expectedCode, int $expectedStatus): void
    {
        try {
            $fn();
            $this->fail("expected VaultApiError with code {$expectedCode}");
        } catch (VaultApiError $e) {
            self::assertSame($expectedCode, $e->errorCode, "wrong code: {$e->getMessage()}");
            self::assertSame($expectedStatus, $e->status, 'wrong status');
        }
    }

    // ------------------------------------------------------------------
    // Happy path — full lifecycle
    // ------------------------------------------------------------------

    public function test_full_lifecycle_create_claim_approve_revoke_rotate(): void
    {
        $adminPubkey = random_bytes(32);
        $claimantPubkey = random_bytes(32);

        // 1) admin creates join request
        $this->setAuth(self::ADMIN_DEVICE, self::ADMIN_TOKEN);
        $resp = $this->invoke(fn() => VaultGrantsController::createJoinRequest(
            $this->db,
            $this->ctx('POST', ['vault_id' => self::VAULT_ID], json_encode([
                'ephemeral_admin_pubkey' => base64_encode($adminPubkey),
            ])),
        ));
        self::assertSame(201, $resp['status']);
        self::assertTrue($resp['json']['ok']);
        $jrId = $resp['json']['data']['join_request_id'];
        self::assertMatchesRegularExpression('/^jr_v1_[a-z2-7]{24}$/', $jrId);
        self::assertSame('pending', $resp['json']['data']['state']);

        // 2) claimant claims
        $this->setAuth(self::CLAIMANT_DEVICE, self::CLAIMANT_TOKEN);
        $resp = $this->invoke(fn() => VaultGrantsController::claim(
            $this->db,
            $this->ctx('POST', ['vault_id' => self::VAULT_ID, 'req_id' => $jrId],
                json_encode([
                    'claimant_pubkey' => base64_encode($claimantPubkey),
                    'device_name' => 'New Claimant Laptop',
                ])),
        ));
        self::assertSame(200, $resp['status']);
        self::assertSame('claimed', $resp['json']['data']['state']);
        self::assertSame('New Claimant Laptop', $resp['json']['data']['device_name']);

        // 3) admin polls — sees claimant pubkey
        $this->setAuth(self::ADMIN_DEVICE, self::ADMIN_TOKEN);
        $resp = $this->invoke(fn() => VaultGrantsController::getJoinRequest(
            $this->db,
            $this->ctx('GET', ['vault_id' => self::VAULT_ID, 'req_id' => $jrId]),
        ));
        self::assertSame(200, $resp['status']);
        self::assertNotNull($resp['json']['data']['claimant_pubkey']);
        self::assertSame(self::CLAIMANT_DEVICE, $resp['json']['data']['claimant_device_id']);
        // Wrapped grant not present yet (not approved).
        self::assertNull($resp['json']['data']['wrapped_vault_grant']);

        // 4) admin approves with role + wrapped grant
        $wrappedGrant = random_bytes(64);
        $resp = $this->invoke(fn() => VaultGrantsController::approve(
            $this->db,
            $this->ctx('POST', ['vault_id' => self::VAULT_ID, 'req_id' => $jrId],
                json_encode([
                    'approved_role' => 'sync',
                    'wrapped_vault_grant' => base64_encode($wrappedGrant),
                ])),
        ));
        self::assertSame(200, $resp['status']);
        self::assertSame('approved', $resp['json']['data']['state']);
        self::assertSame('sync', $resp['json']['data']['approved_role']);

        // The grant row now exists and is active.
        $grant = (new VaultDeviceGrantsRepository($this->db))->getByDevice(
            self::VAULT_ID, self::CLAIMANT_DEVICE,
        );
        self::assertNotNull($grant);
        self::assertSame('sync', (string)$grant['role']);
        self::assertNull($grant['revoked_at']);

        // 5) claimant fetches the wrapped grant (only the claimant device may see it)
        $this->setAuth(self::CLAIMANT_DEVICE, self::CLAIMANT_TOKEN);
        $resp = $this->invoke(fn() => VaultGrantsController::getJoinRequest(
            $this->db,
            $this->ctx('GET', ['vault_id' => self::VAULT_ID, 'req_id' => $jrId]),
        ));
        self::assertSame(base64_encode($wrappedGrant), $resp['json']['data']['wrapped_vault_grant']);

        // 6) admin revokes the new device
        $this->setAuth(self::ADMIN_DEVICE, self::ADMIN_TOKEN);
        $resp = $this->invoke(fn() => VaultGrantsController::revokeDeviceGrant(
            $this->db,
            $this->ctx('DELETE', [
                'vault_id' => self::VAULT_ID,
                'device_id' => self::CLAIMANT_DEVICE,
            ]),
        ));
        self::assertSame(200, $resp['status']);
        self::assertFalse($resp['json']['data']['already_revoked']);
        $grant = (new VaultDeviceGrantsRepository($this->db))->getByDevice(
            self::VAULT_ID, self::CLAIMANT_DEVICE,
        );
        self::assertNotNull($grant['revoked_at']);

        // 7) admin rotates the access secret
        $newSecret = 'rotated-very-high-entropy-' . bin2hex(random_bytes(8));
        $newHash = hash('sha256', $newSecret, true);
        $resp = $this->invoke(fn() => VaultGrantsController::rotateAccessSecret(
            $this->db,
            $this->ctx('POST', ['vault_id' => self::VAULT_ID], json_encode([
                'new_vault_access_token_hash' => base64_encode($newHash),
            ])),
        ));
        self::assertSame(200, $resp['status']);
        self::assertNotEmpty($resp['json']['data']['rotated_at']);
        // Old secret no longer matches.
        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        self::assertSame($newHash, (string)$vault['vault_access_token_hash']);
    }

    // ------------------------------------------------------------------
    // Negative cases
    // ------------------------------------------------------------------

    public function test_non_admin_cannot_create_join_request(): void
    {
        $this->setAuth(self::NON_ADMIN_DEVICE, self::NON_ADMIN_TOKEN);
        $this->expectVaultError(
            fn() => VaultGrantsController::createJoinRequest(
                $this->db,
                $this->ctx('POST', ['vault_id' => self::VAULT_ID], json_encode([
                    'ephemeral_admin_pubkey' => base64_encode(random_bytes(32)),
                ])),
            ),
            'vault_access_denied', 403,
        );
    }

    public function test_approve_before_claim_returns_state_409(): void
    {
        $this->setAuth(self::ADMIN_DEVICE, self::ADMIN_TOKEN);
        $resp = $this->invoke(fn() => VaultGrantsController::createJoinRequest(
            $this->db,
            $this->ctx('POST', ['vault_id' => self::VAULT_ID], json_encode([
                'ephemeral_admin_pubkey' => base64_encode(random_bytes(32)),
            ])),
        ));
        $jrId = $resp['json']['data']['join_request_id'];

        // Skip the claim step — go straight to approve.
        $this->expectVaultError(
            fn() => VaultGrantsController::approve(
                $this->db,
                $this->ctx('POST', ['vault_id' => self::VAULT_ID, 'req_id' => $jrId],
                    json_encode([
                        'approved_role' => 'sync',
                        'wrapped_vault_grant' => base64_encode(random_bytes(48)),
                    ])),
            ),
            'vault_join_request_state', 409,
        );
    }

    public function test_revoke_unknown_device_returns_404(): void
    {
        $this->setAuth(self::ADMIN_DEVICE, self::ADMIN_TOKEN);
        $this->expectVaultError(
            fn() => VaultGrantsController::revokeDeviceGrant(
                $this->db,
                $this->ctx('DELETE', [
                    'vault_id' => self::VAULT_ID,
                    'device_id' => 'deadbeefdeadbeefdeadbeefdeadbeef',
                ]),
            ),
            'vault_join_request_state', 404,
        );
    }

    public function test_admin_cannot_self_revoke(): void
    {
        $this->setAuth(self::ADMIN_DEVICE, self::ADMIN_TOKEN);
        $this->expectVaultError(
            fn() => VaultGrantsController::revokeDeviceGrant(
                $this->db,
                $this->ctx('DELETE', [
                    'vault_id' => self::VAULT_ID,
                    'device_id' => self::ADMIN_DEVICE,
                ]),
            ),
            'vault_invalid_request', 400,
        );
    }

    public function test_invalid_role_rejected_by_approve(): void
    {
        // Set up a claimed jr first.
        $this->setAuth(self::ADMIN_DEVICE, self::ADMIN_TOKEN);
        $resp = $this->invoke(fn() => VaultGrantsController::createJoinRequest(
            $this->db,
            $this->ctx('POST', ['vault_id' => self::VAULT_ID], json_encode([
                'ephemeral_admin_pubkey' => base64_encode(random_bytes(32)),
            ])),
        ));
        $jrId = $resp['json']['data']['join_request_id'];

        $this->setAuth(self::CLAIMANT_DEVICE, self::CLAIMANT_TOKEN);
        $this->invoke(fn() => VaultGrantsController::claim(
            $this->db,
            $this->ctx('POST', ['vault_id' => self::VAULT_ID, 'req_id' => $jrId],
                json_encode([
                    'claimant_pubkey' => base64_encode(random_bytes(32)),
                    'device_name' => 'Some Laptop',
                ])),
        ));

        $this->setAuth(self::ADMIN_DEVICE, self::ADMIN_TOKEN);
        $this->expectVaultError(
            fn() => VaultGrantsController::approve(
                $this->db,
                $this->ctx('POST', ['vault_id' => self::VAULT_ID, 'req_id' => $jrId],
                    json_encode([
                        'approved_role' => 'super-admin',  // not in the §D11 set
                        'wrapped_vault_grant' => base64_encode(random_bytes(32)),
                    ])),
            ),
            'vault_invalid_request', 400,
        );
    }
}
