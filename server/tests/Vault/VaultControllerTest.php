<?php

declare(strict_types=1);

use PHPUnit\Framework\Attributes\RunTestsInSeparateProcesses;
use PHPUnit\Framework\TestCase;

/**
 * Integration tests for VaultController (T1.6). Each of the 12 endpoints
 * gets a happy-path test and at least one error-case test that exercises
 * a code from the T0 §"Error codes" table.
 *
 * Tests run in separate processes so http_response_code() and header()
 * calls don't bleed across cases. setUp seeds:
 *   - a temp-file SQLite DB with the migration applied,
 *   - a registered device + a vault (post-create state),
 *   - a temp storage root via VaultStorage::setRoot.
 *
 * The "controller invocation" helper opens an output buffer, calls the
 * static method, and returns
 *   ['status' => int, 'json' => array|null, 'raw' => string]
 * so each test can assert against the wire shape directly. Request bodies
 * are stubbed via RequestContext::bodyOverride (no php://input wrapper
 * hack — see the constructor in src/Http/RequestContext.php).
 */
#[RunTestsInSeparateProcesses]
final class VaultControllerTest extends TestCase
{
    private string $dbPath;
    private string $storageRoot;
    private Database $db;

    // Valid RFC 4648 base32: only A–Z + 2–7. 8 and 9 are not in the alphabet.
    private const VAULT_ID_DASHED = 'ABCD-2345-WXYZ';
    private const VAULT_ID        = 'ABCD2345WXYZ';
    private const VAULT_SECRET    = 'super-high-entropy-secret';
    private const HEADER_HASH     = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const ROOT_HASH       = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const FOLDER_A        = 'rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa';
    private const FOLDER_B        = 'rf_v1_bbbbbbbbbbbbbbbbbbbbbbbb';
    private const NOW             = 1714680000;

    private const DEVICE_ID    = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
    private const DEVICE_TOKEN = 'device-bearer-token';

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_ctl_db_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();

        $this->storageRoot = sys_get_temp_dir() . '/vault_ctl_storage_' . bin2hex(random_bytes(4));
        mkdir($this->storageRoot, 0700, true);
        VaultStorage::setRoot($this->storageRoot);

        // Register a device that vault auth can match against.
        (new DeviceRepository($this->db))->insertDevice(
            self::DEVICE_ID,
            base64_encode(random_bytes(32)),
            self::DEVICE_TOKEN,
            'desktop',
            self::NOW
        );

        // Seed a vault in the post-create state (handy for read endpoints
        // and CAS tests). Tests that exercise create() are responsible for
        // clearing this pre-state themselves.
        (new VaultsRepository($this->db))->create(
            self::VAULT_ID,
            hash('sha256', self::VAULT_SECRET, true),
            "\x01header",
            self::HEADER_HASH,
            self::ROOT_HASH,
            self::NOW
        );
        (new VaultRootManifestsRepository($this->db))->create(
            self::VAULT_ID,
            1,
            0,
            self::ROOT_HASH,
            "\x02root",
            5,
            self::DEVICE_ID,
            self::NOW
        );

        // The role-matrix gate (§D11) requires every authenticated write to
        // come from a device that holds an active grant. VaultController::create
        // auto-inserts the creator's admin grant; this fixture seeds the vault
        // directly via the repo so the equivalent grant is added by hand.
        (new VaultDeviceGrantsRepository($this->db))->insertGrant(
            'gr_v1_seedadmin000000000000aa',
            self::VAULT_ID,
            self::DEVICE_ID,
            'Test Admin Desktop',
            'admin',
            self::DEVICE_ID,
            'create',
            self::NOW,
        );

        $this->setAuth();
    }

    protected function tearDown(): void
    {
        VaultStorage::setRoot(null);
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
        $this->rrmdir($this->storageRoot);
        foreach ([
            'HTTP_X_DEVICE_ID', 'HTTP_AUTHORIZATION',
            'HTTP_X_VAULT_ID', 'HTTP_X_VAULT_AUTHORIZATION',
            'REQUEST_URI', 'REQUEST_METHOD',
        ] as $k) {
            unset($_SERVER[$k]);
        }
    }

    private function rrmdir(string $dir): void
    {
        if (!is_dir($dir)) return;
        foreach (scandir($dir) as $f) {
            if ($f === '.' || $f === '..') continue;
            $p = $dir . '/' . $f;
            is_dir($p) ? $this->rrmdir($p) : @unlink($p);
        }
        @rmdir($dir);
    }

    private function setAuth(): void
    {
        $_SERVER['HTTP_X_DEVICE_ID']   = self::DEVICE_ID;
        $_SERVER['HTTP_AUTHORIZATION'] = 'Bearer ' . self::DEVICE_TOKEN;
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;
    }

    /**
     * Invoke a controller closure, capture status + body, return a
     * structured result. Closure must be allowed to throw; the caller
     * handles error-case tests via try/catch instead.
     */
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

    private function ctx(string $method, array $params = [], ?string $body = null): RequestContext
    {
        return new RequestContext(method: $method, params: $params, bodyOverride: $body);
    }

    /** Convenience: ctx with a JSON-encoded body. */
    private function jctx(string $method, array $params, array $body): RequestContext
    {
        return $this->ctx($method, $params, json_encode($body));
    }

    /**
     * Build a syntactically-correct manifest envelope (formats §10.1).
     * The relay parses the first 61 bytes for CAS, so tests need a real
     * envelope shape — the AEAD bytes themselves don't have to verify.
     *
     * F-T11: ``aeadCiphertextAndTag`` is intentionally an opaque
     * placeholder ("stub-ciphertext") because the relay is blind: it
     * never decrypts the manifest — only the deterministic 61-byte
     * prefix governs CAS, hash, and quota accounting. If a future
     * relay-side verifier *is* added (e.g. a hash precondition
     * cross-check that needs the AEAD bytes), every stub helper here
     * starts failing — that's the point. The break tells the next
     * contributor "you changed the relay's AEAD posture; stop and
     * write fixtures with a real round-trip via VaultCrypto::aeadEncrypt."
     * For an existing real-AEAD round-trip baseline see
     * ``test_full_aead_round_trip_smoke`` below.
     */
    private function rootEnvelope(
        int $rootRevision,
        int $parentRootRevision,
        string $authorDeviceId = self::DEVICE_ID,
        string $vaultId = self::VAULT_ID,
        string $aeadCiphertextAndTag = "stub-ciphertext"
    ): string {
        return VaultCrypto::buildRootEnvelope(
            $vaultId,
            $rootRevision,
            $parentRootRevision,
            $authorDeviceId,
            str_repeat("\0", 24),
            $aeadCiphertextAndTag,
        );
    }

    private function shardEnvelope(
        string $remoteFolderId,
        int $shardRevision,
        int $parentShardRevision,
        string $authorDeviceId = self::DEVICE_ID,
        string $vaultId = self::VAULT_ID,
        string $aeadCiphertextAndTag = "stub-ciphertext"
    ): string {
        return VaultCrypto::buildShardEnvelope(
            $vaultId,
            $remoteFolderId,
            $shardRevision,
            $parentShardRevision,
            $authorDeviceId,
            str_repeat("\0", 24),
            $aeadCiphertextAndTag,
        );
    }

    /**
     * Build a syntactically-correct header envelope (formats §9.1).
     * Same F-T11 stub-AEAD contract as :func:`manifestEnvelope`.
     */
    private function headerEnvelope(
        int $headerRevision,
        string $vaultId = self::VAULT_ID,
        string $aeadCiphertextAndTag = "stub-ciphertext"
    ): string {
        return VaultCrypto::buildHeaderEnvelope(
            $vaultId,
            $headerRevision,
            str_repeat("\0", 24),
            $aeadCiphertextAndTag,
        );
    }

    /**
     * Pre-uploads a chunk. Used by tests that need a chunk to exist.
     * Wraps in ob_start so the controller's echo doesn't leak into the
     * test process's stdout (PHPUnit `failOnRisky` flags that as risky).
     */
    private function uploadChunk(string $chunkId, string $bytes): void
    {
        ob_start();
        try {
            VaultController::putChunk(
                $this->db,
                $this->ctx(
                    'PUT',
                    ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId],
                    $bytes,
                )
            );
        } finally {
            ob_end_clean();
        }
    }

    // ===================================================================
    //  F-T11 — real AEAD round-trip baseline
    // ===================================================================

    /**
     * F-T11: pin a real AEAD round-trip so the suite has at least one
     * fixture that exercises ``VaultCrypto::aeadEncrypt`` /
     * ``aeadDecrypt`` end-to-end. The other tests in this file build
     * envelopes with ``aeadCiphertextAndTag = "stub-ciphertext"``
     * because the relay never decrypts — only the deterministic
     * 61-byte prefix governs CAS. Without this baseline a future
     * relay-side verifier addition would silently pass every
     * stub-using test (relay can't tell stub bytes from real bytes
     * because it doesn't decrypt at all today). The smoke test below
     * is the canary: if ``aeadEncrypt`` ever drifts from
     * ``aeadDecrypt``, this fails alone, signalling that the stub
     * helpers above need refreshing too.
     */
    public function test_full_aead_round_trip_smoke(): void
    {
        $masterKey = random_bytes(32);
        $nonce = random_bytes(24);
        $plaintext = json_encode([
            'schema' => 'dc-vault-root-v1',
            'root_revision' => 5,
            'parent_root_revision' => 4,
            'remote_folders' => [],
        ]);

        $aad = VaultCrypto::buildRootAad(
            self::VAULT_ID, 5, 4, self::DEVICE_ID
        );
        $subkey = VaultCrypto::deriveSubkey('dc-vault-v1/root', $masterKey);
        $ct = VaultCrypto::aeadEncrypt($plaintext, $subkey, $nonce, $aad);
        // Decrypt round-trip must return the original bytes byte-exact.
        self::assertSame(
            $plaintext,
            VaultCrypto::aeadDecrypt($ct, $subkey, $nonce, $aad),
            'F-T11: real AEAD round-trip must succeed; if this fails the '
            . 'stub-ciphertext helpers above are no longer a safe shorthand'
        );
        // And a tampered byte must trip the AEAD tag check.
        $tampered = $ct;
        $tampered[0] = chr(ord($tampered[0]) ^ 0x01);
        $this->expectException(SodiumException::class);
        VaultCrypto::aeadDecrypt($tampered, $subkey, $nonce, $aad);
    }

    // ===================================================================
    //  6.1 POST /api/vaults
    // ===================================================================

    public function test_create_happy_path(): void
    {
        $this->db->execute('DELETE FROM vaults');
        $this->db->execute('DELETE FROM vault_root_manifests');
        $this->db->execute('DELETE FROM vault_folder_shards');
        $this->db->execute('DELETE FROM vault_folder_shard_heads');

        // §1.H4: create now parses the envelope prefix and enforces
        // format_version + (vault_id, revision) match. Build proper
        // envelopes via the existing test helpers.
        $body = [
            'vault_id'                => self::VAULT_ID_DASHED,
            'vault_access_token_hash' => base64_encode(hash('sha256', 'fresh-secret', true)),
            'encrypted_header'        => base64_encode($this->headerEnvelope(1)),
            'header_hash'             => self::HEADER_HASH,
            'initial_root_ciphertext' => base64_encode($this->rootEnvelope(1, 0)),
            'initial_root_hash'       => self::ROOT_HASH,
        ];

        $res = $this->invoke(fn() => VaultController::create(
            $this->db, $this->jctx('POST', [], $body)
        ));

        self::assertSame(201, $res['status']);
        self::assertTrue($res['json']['ok']);
        self::assertSame(self::VAULT_ID_DASHED, $res['json']['data']['vault_id']);
        self::assertSame(1, $res['json']['data']['header_revision']);
        self::assertSame(1, $res['json']['data']['root_revision']);
        self::assertSame(1073741824, $res['json']['data']['quota_ciphertext_bytes']);
        self::assertSame(0, $res['json']['data']['used_ciphertext_bytes']);
    }

    public function test_create_returns_409_vault_already_exists(): void
    {
        $body = [
            'vault_id'                => self::VAULT_ID_DASHED,
            'vault_access_token_hash' => base64_encode(hash('sha256', 'x', true)),
            'encrypted_header'        => base64_encode($this->headerEnvelope(1)),
            'header_hash'             => self::HEADER_HASH,
            'initial_root_ciphertext' => base64_encode($this->rootEnvelope(1, 0)),
            'initial_root_hash'       => self::ROOT_HASH,
        ];

        try {
            VaultController::create($this->db, $this->jctx('POST', [], $body));
            self::fail('expected VaultAlreadyExistsError');
        } catch (VaultAlreadyExistsError $e) {
            self::assertSame(409, $e->status);
            self::assertSame('vault_already_exists', $e->errorCode);
            self::assertSame(self::VAULT_ID, $e->details['vault_id']);
        }
    }

    /**
     * Review §1.H4: create now refuses an encrypted_header whose
     * sealed vault_id disagrees with the path vault_id (mirrors
     * putHeader's check). Pre-fix the mismatched bytes were
     * persisted into the row.
     */
    public function test_create_422_when_header_envelope_vault_id_mismatch(): void
    {
        $this->db->execute('DELETE FROM vaults');
        $this->db->execute('DELETE FROM vault_root_manifests');
        $this->db->execute('DELETE FROM vault_folder_shards');
        $this->db->execute('DELETE FROM vault_folder_shard_heads');

        // Envelope sealed to a DIFFERENT vault_id, request body points
        // at VAULT_ID.
        $body = [
            'vault_id'                => self::VAULT_ID_DASHED,
            'vault_access_token_hash' => base64_encode(hash('sha256', 'fresh', true)),
            'encrypted_header'        => base64_encode(
                $this->headerEnvelope(1, vaultId: 'OTHERVAULTABC')
            ),
            'header_hash'             => self::HEADER_HASH,
            'initial_root_ciphertext' => base64_encode($this->rootEnvelope(1, 0)),
            'initial_root_hash'       => self::ROOT_HASH,
        ];
        try {
            VaultController::create(
                $this->db, $this->jctx('POST', [], $body)
            );
            self::fail('expected VaultHeaderTamperedError');
        } catch (VaultHeaderTamperedError $e) {
            self::assertSame(422, $e->status);
        }
    }

    /**
     * Review §1.M1: hash fields MUST be 64 lowercase hex chars. Pre-fix
     * any non-empty string passed validation and "banana" would survive
     * storage to surface in 409 `current_*_hash` payloads as opaque
     * garbage.
     */
    public function test_create_400_on_malformed_header_hash(): void
    {
        $this->db->execute('DELETE FROM vaults');
        $this->db->execute('DELETE FROM vault_root_manifests');
        $this->db->execute('DELETE FROM vault_folder_shards');
        $this->db->execute('DELETE FROM vault_folder_shard_heads');

        $body = [
            'vault_id'                => self::VAULT_ID_DASHED,
            'vault_access_token_hash' => base64_encode(hash('sha256', 'fresh', true)),
            'encrypted_header'        => base64_encode($this->headerEnvelope(1)),
            'header_hash'             => 'banana',  // not hex64
            'initial_root_ciphertext' => base64_encode($this->rootEnvelope(1, 0)),
            'initial_root_hash'       => self::ROOT_HASH,
        ];
        try {
            VaultController::create(
                $this->db, $this->jctx('POST', [], $body)
            );
            self::fail('expected VaultInvalidRequestError');
        } catch (VaultInvalidRequestError $e) {
            self::assertSame(400, $e->status);
            self::assertSame('header_hash', $e->details['field']);
            self::assertStringContainsString('hex', $e->getMessage());
        }
    }

    /**
     * Review §1.H4: a v0x02 envelope at create-time must 422 before
     * the row lands — same gate that already applies on putHeader.
     */
    public function test_create_422_on_unsupported_header_format_version(): void
    {
        $this->db->execute('DELETE FROM vaults');
        $this->db->execute('DELETE FROM vault_root_manifests');
        $this->db->execute('DELETE FROM vault_folder_shards');
        $this->db->execute('DELETE FROM vault_folder_shard_heads');

        $badHeader = $this->tamperFormatVersion($this->headerEnvelope(1), 0x02);
        $body = [
            'vault_id'                => self::VAULT_ID_DASHED,
            'vault_access_token_hash' => base64_encode(hash('sha256', 'fresh', true)),
            'encrypted_header'        => base64_encode($badHeader),
            'header_hash'             => self::HEADER_HASH,
            'initial_root_ciphertext' => base64_encode($this->rootEnvelope(1, 0)),
            'initial_root_hash'       => self::ROOT_HASH,
        ];
        try {
            VaultController::create(
                $this->db, $this->jctx('POST', [], $body)
            );
            self::fail('expected VaultFormatVersionUnsupportedError');
        } catch (VaultFormatVersionUnsupportedError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_format_version_unsupported', $e->errorCode);
        }
    }

    public function test_create_400_on_invalid_vault_id(): void
    {
        $body = [
            'vault_id'                => 'not-a-vault-id',
            'vault_access_token_hash' => base64_encode(hash('sha256', 'x', true)),
            'encrypted_header'        => base64_encode('h'),
            'header_hash'             => self::HEADER_HASH,
            'initial_root_ciphertext' => base64_encode('r'),
            'initial_root_hash'       => self::ROOT_HASH,
        ];

        $this->expectException(VaultInvalidRequestError::class);
        VaultController::create($this->db, $this->jctx('POST', [], $body));
    }

    // ===================================================================
    //  6.2 GET /api/vaults/{id}/header
    // ===================================================================

    public function test_getHeader_happy_path(): void
    {
        $res = $this->invoke(fn() => VaultController::getHeader(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID])
        ));

        self::assertSame(200, $res['status']);
        self::assertTrue($res['json']['ok']);
        self::assertSame(self::VAULT_ID_DASHED, $res['json']['data']['vault_id']);
        self::assertSame(self::HEADER_HASH, $res['json']['data']['header_hash']);
        self::assertSame(1, $res['json']['data']['header_revision']);
        self::assertSame(1073741824, $res['json']['data']['quota_ciphertext_bytes']);
        self::assertNull($res['json']['data']['migrated_to']);
    }

    public function test_getHeader_404_vault_not_found(): void
    {
        $this->expectException(VaultNotFoundError::class);
        VaultController::getHeader($this->db, $this->ctx('GET', ['vault_id' => 'AAAAAAAAAAAA']));
    }

    // ===================================================================
    //  6.3 PUT /api/vaults/{id}/header (CAS)
    // ===================================================================

    public function test_putHeader_happy_path_bumps_revision(): void
    {
        $newHash = str_repeat('a', 64);
        $body = [
            'expected_header_revision' => 1,
            'new_header_revision'      => 2,
            'encrypted_header'         => base64_encode($this->headerEnvelope(2)),
            'header_hash'              => $newHash,
        ];

        $res = $this->invoke(fn() => VaultController::putHeader(
            $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(2, $res['json']['data']['header_revision']);
        self::assertSame($newHash, $res['json']['data']['header_hash']);
    }

    public function test_putHeader_409_on_revision_mismatch(): void
    {
        $body = [
            'expected_header_revision' => 99,
            'new_header_revision'      => 100,
            'encrypted_header'         => base64_encode($this->headerEnvelope(100)),
            'header_hash'              => str_repeat('b', 64),
        ];

        try {
            VaultController::putHeader($this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body));
            self::fail('expected VaultManifestConflictError');
        } catch (VaultManifestConflictError $e) {
            self::assertSame(409, $e->status);
            self::assertSame('vault_manifest_conflict', $e->errorCode);
            self::assertSame(1, $e->details['current_revision']);
            self::assertSame(99, $e->details['expected_revision']);
        }
    }

    public function test_putHeader_400_when_envelope_revision_disagrees(): void
    {
        // Body says revision=2 but envelope says revision=99.
        $body = [
            'expected_header_revision' => 1,
            'new_header_revision'      => 2,
            'encrypted_header'         => base64_encode($this->headerEnvelope(99)),
            'header_hash'              => str_repeat('a', 64),
        ];
        try {
            VaultController::putHeader($this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body));
            self::fail('expected VaultHeaderTamperedError');
        } catch (VaultHeaderTamperedError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_header_tampered', $e->errorCode);
        }
    }

    public function test_putHeader_400_on_url_safe_base64_encoded_header(): void
    {
        // F-S22: vault wire format pins RFC 4648 §4 alphabet (`+/`). A body
        // that snuck through with URL-safe `-_` chars must 400 before
        // base64_decode runs, so a future hash test that compares against
        // the spec'd alphabet doesn't quietly diverge.
        $standard = base64_encode($this->headerEnvelope(2));
        // Mangle into URL-safe shape: replace any `+/` with `-_`. If the
        // sample doesn't contain either, manually inject one URL-safe char.
        $urlSafe = strtr($standard, ['+' => '-', '/' => '_']);
        if ($urlSafe === $standard) {
            // Force the divergence so the test is meaningful even when the
            // sample bytes happened to avoid `+` and `/`.
            $urlSafe = '-' . substr($standard, 1);
        }
        $body = [
            'expected_header_revision' => 1,
            'new_header_revision'      => 2,
            'encrypted_header'         => $urlSafe,
            'header_hash'              => str_repeat('a', 64),
        ];

        try {
            VaultController::putHeader($this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body));
            self::fail('expected VaultInvalidRequestError');
        } catch (VaultInvalidRequestError $e) {
            self::assertSame(400, $e->status);
            self::assertSame('vault_invalid_request', $e->errorCode);
            self::assertSame('encrypted_header', $e->details['field'] ?? null);
        }
    }

    // ===================================================================
    //  6.4 GET /api/vaults/{id}/root
    // ===================================================================

    public function test_getRoot_happy_path(): void
    {
        $res = $this->invoke(fn() => VaultController::getRoot(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID])
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(1, $res['json']['data']['root_revision']);
        self::assertSame(0, $res['json']['data']['parent_root_revision']);
        self::assertSame(self::ROOT_HASH, $res['json']['data']['root_hash']);
        self::assertSame(base64_encode("\x02root"), $res['json']['data']['root_ciphertext']);
        self::assertSame(5, $res['json']['data']['root_size']);
    }

    public function test_getRoot_404_unknown_vault(): void
    {
        $this->expectException(VaultNotFoundError::class);
        VaultController::getRoot($this->db, $this->ctx('GET', ['vault_id' => 'AAAAAAAAAAAA']));
    }

    // ===================================================================
    //  6.6 PUT /api/vaults/{id}/root (CAS, §A1-root conflict)
    // ===================================================================

    public function test_putRoot_happy_path_advances_head(): void
    {
        $newHash = str_repeat('1', 64);
        $body = [
            'expected_current_root_revision' => 1,
            'new_root_revision'              => 2,
            'parent_root_revision'           => 1,
            'root_hash'                      => $newHash,
            'root_ciphertext'                => base64_encode($this->rootEnvelope(2, 1)),
        ];

        $res = $this->invoke(fn() => VaultController::putRoot(
            $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(2, $res['json']['data']['root_revision']);
        self::assertSame($newHash, $res['json']['data']['root_hash']);
    }

    public function test_putRoot_422_when_envelope_revision_disagrees(): void
    {
        $body = [
            'expected_current_root_revision' => 1,
            'new_root_revision'              => 2,
            'parent_root_revision'           => 1,
            'root_hash'                      => str_repeat('1', 64),
            'root_ciphertext'                => base64_encode($this->rootEnvelope(99, 98)),
        ];
        try {
            VaultController::putRoot(
                $this->db,
                $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultRootTamperedError');
        } catch (VaultRootTamperedError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_root_tampered', $e->errorCode);
        }
    }

    public function test_putRoot_422_when_envelope_author_disagrees(): void
    {
        $body = [
            'expected_current_root_revision' => 1,
            'new_root_revision'              => 2,
            'parent_root_revision'           => 1,
            'root_hash'                      => str_repeat('1', 64),
            'root_ciphertext'                => base64_encode($this->rootEnvelope(
                2, 1, str_repeat('f', 32)
            )),
        ];
        try {
            VaultController::putRoot(
                $this->db,
                $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultRootTamperedError');
        } catch (VaultRootTamperedError $e) {
            self::assertSame(422, $e->status);
            self::assertStringContainsString('author_device_id', $e->details['reason']);
        }
    }

    public function test_putRoot_409_a1_root_conflict_payload(): void
    {
        $body = [
            'expected_current_root_revision' => 99,
            'new_root_revision'              => 100,
            'parent_root_revision'           => 99,
            'root_hash'                      => str_repeat('2', 64),
            'root_ciphertext'                => base64_encode($this->rootEnvelope(100, 99)),
        ];

        try {
            VaultController::putRoot($this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body));
            self::fail('expected VaultRootConflictError with A1-root payload');
        } catch (VaultRootConflictError $e) {
            self::assertSame('vault_root_conflict', $e->errorCode);
            self::assertSame(1, $e->details['current_root_revision']);
            self::assertSame(99, $e->details['expected_root_revision']);
            self::assertSame(self::ROOT_HASH, $e->details['current_root_hash']);
            self::assertSame(base64_encode("\x02root"), $e->details['current_root_ciphertext']);
            self::assertSame(5, $e->details['current_root_size']);
        }
    }

    // ===================================================================
    //  6.5 GET /api/vaults/{id}/folders/{folder_id}/shard
    //  6.7 PUT /api/vaults/{id}/folders/{folder_id}/shard
    //  6.8 PUT /api/vaults/{id}/folders/{folder_id}/shard-with-root
    // ===================================================================

    public function test_putShardWithRoot_genesis_shard_for_new_folder(): void
    {
        $shardHash = str_repeat('a', 64);
        $rootHash  = str_repeat('b', 64);
        $body = [
            'shard' => [
                'expected_current_shard_revision' => 0,
                'new_shard_revision'              => 1,
                'parent_shard_revision'           => 0,
                'shard_hash'                      => $shardHash,
                'shard_ciphertext'                => base64_encode($this->shardEnvelope(self::FOLDER_A, 1, 0)),
            ],
            'root' => [
                'expected_current_root_revision' => 1,
                'new_root_revision'              => 2,
                'parent_root_revision'           => 1,
                'root_hash'                      => $rootHash,
                'root_ciphertext'                => base64_encode($this->rootEnvelope(2, 1)),
            ],
        ];

        $res = $this->invoke(fn() => VaultController::putShardWithRoot(
            $this->db,
            $this->jctx('PUT', [
                'vault_id' => self::VAULT_ID, 'folder_id' => self::FOLDER_A,
            ], $body),
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(1, $res['json']['data']['shard_revision']);
        self::assertSame(2, $res['json']['data']['root_revision']);
        self::assertSame($shardHash, $res['json']['data']['shard_hash']);
        self::assertSame($rootHash, $res['json']['data']['root_hash']);
    }

    public function test_getShard_after_atomic_publish(): void
    {
        $this->seedShard(self::FOLDER_A, 1, 0, 'SHARD-CONTENT');
        $res = $this->invoke(fn() => VaultController::getShard(
            $this->db,
            $this->ctx('GET', ['vault_id' => self::VAULT_ID, 'folder_id' => self::FOLDER_A]),
        ));
        self::assertSame(200, $res['status']);
        self::assertSame(self::FOLDER_A, $res['json']['data']['remote_folder_id']);
        self::assertSame(1, $res['json']['data']['shard_revision']);
        self::assertSame(base64_encode('SHARD-CONTENT'), $res['json']['data']['shard_ciphertext']);
    }

    public function test_getShard_404_unknown_folder(): void
    {
        $this->expectException(VaultNotFoundError::class);
        VaultController::getShard($this->db, $this->ctx('GET', [
            'vault_id' => self::VAULT_ID, 'folder_id' => self::FOLDER_B,
        ]));
    }

    public function test_putShardWithRoot_409_shard_root_conflict_when_both_stale(): void
    {
        // Advance shard + root out-of-band so the request's expectations
        // are stale on both sides.
        $this->seedShard(self::FOLDER_A, 1, 0, 'EXISTING');
        (new VaultRootManifestsRepository($this->db))->tryCAS(
            self::VAULT_ID, 1, 2, str_repeat('z', 64), 'NEWROOT', 7, self::DEVICE_ID, self::NOW + 5,
        );

        $body = [
            'shard' => [
                'expected_current_shard_revision' => 0,
                'new_shard_revision'              => 1,
                'parent_shard_revision'           => 0,
                'shard_hash'                      => str_repeat('a', 64),
                'shard_ciphertext'                => base64_encode($this->shardEnvelope(self::FOLDER_A, 1, 0)),
            ],
            'root' => [
                'expected_current_root_revision' => 1,
                'new_root_revision'              => 2,
                'parent_root_revision'           => 1,
                'root_hash'                      => str_repeat('b', 64),
                'root_ciphertext'                => base64_encode($this->rootEnvelope(2, 1)),
            ],
        ];

        try {
            VaultController::putShardWithRoot(
                $this->db,
                $this->jctx('PUT', [
                    'vault_id' => self::VAULT_ID, 'folder_id' => self::FOLDER_A,
                ], $body),
            );
            self::fail('expected VaultShardRootConflictError');
        } catch (VaultShardRootConflictError $e) {
            self::assertSame('vault_shard_root_conflict', $e->errorCode);
            self::assertSame(self::FOLDER_A, $e->details['remote_folder_id']);
            self::assertSame(1, $e->details['current_shard_revision']);
            self::assertSame(2, $e->details['current_root_revision']);
        }
    }

    public function test_putShard_accepts_migration_genesis_at_arbitrary_rev(): void
    {
        // §5.M2: migration replicates a source-side shard verbatim
        // into a fresh target. Body has expected=0 (no shard yet on
        // target), new_shard_revision=N (whatever the source was at),
        // parent_shard_revision=N-1. The envelope's deterministic
        // prefix matches new + parent — same envelope-vs-body
        // consistency check normal edits satisfy.
        //
        // Server already accepts this: the parent==new-1 check passes
        // (parent=N-1, new=N), and tryCAS sees expected=0 against
        // current=0 (no row) → succeeds. The previously-claimed "server
        // rejects new != expected + 1" check doesn't exist — the only
        // shape constraint on a shard publish is the chain check
        // (parent == new - 1).
        $shardHash = str_repeat('a', 64);
        $body = [
            'expected_current_shard_revision' => 0,
            'new_shard_revision'              => 5,
            'parent_shard_revision'           => 4,
            'shard_hash'                      => $shardHash,
            'shard_ciphertext'                => base64_encode(
                $this->shardEnvelope(self::FOLDER_A, 5, 4),
            ),
        ];

        $res = $this->invoke(fn() => VaultController::putShard(
            $this->db,
            $this->jctx('PUT', [
                'vault_id' => self::VAULT_ID, 'folder_id' => self::FOLDER_A,
            ], $body),
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(5, $res['json']['data']['shard_revision']);
        self::assertSame($shardHash, $res['json']['data']['shard_hash']);

        // Target now has rev=5 stored. A second identical migration
        // POST (idempotent re-entry after a crash) hits the CAS
        // conflict and the client's runner code treats it as a no-op
        // when hash + rev match.
    }

    public function test_putShard_accepts_foreign_envelope_author_on_genesis_insert(): void
    {
        // §5.M2 — genesis-insert (expected=0) is the migration
        // replication path; the envelope is authored by whichever
        // peer wrote it on the source relay, not the migrating
        // device. The server skips the
        // ``env.author_device_id == X-Device-ID`` check here so a
        // multi-device-vault migration succeeds.
        //
        // Non-genesis edits (expected > 0) still enforce the strict
        // match — see ``test_putShard_rejects_foreign_envelope_author_on_edit``.
        $foreignAuthor = str_repeat('b', 32);
        $shardHash = str_repeat('c', 64);
        $body = [
            'expected_current_shard_revision' => 0,
            'new_shard_revision'              => 3,
            'parent_shard_revision'           => 2,
            'shard_hash'                      => $shardHash,
            'shard_ciphertext'                => base64_encode(
                $this->shardEnvelope(self::FOLDER_A, 3, 2, $foreignAuthor),
            ),
        ];

        $res = $this->invoke(fn() => VaultController::putShard(
            $this->db,
            $this->jctx('PUT', [
                'vault_id' => self::VAULT_ID, 'folder_id' => self::FOLDER_A,
            ], $body),
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(3, $res['json']['data']['shard_revision']);
        self::assertSame($shardHash, $res['json']['data']['shard_hash']);
    }

    public function test_putShard_rejects_foreign_envelope_author_on_edit(): void
    {
        // Strict author-match still applies for normal edits
        // (expected > 0). Only the genesis-insert path is relaxed.
        $this->seedShard(self::FOLDER_A, 1, 0, 'SEED');
        $foreignAuthor = str_repeat('b', 32);
        $body = [
            'expected_current_shard_revision' => 1,
            'new_shard_revision'              => 2,
            'parent_shard_revision'           => 1,
            'shard_hash'                      => str_repeat('c', 64),
            'shard_ciphertext'                => base64_encode(
                $this->shardEnvelope(self::FOLDER_A, 2, 1, $foreignAuthor),
            ),
        ];

        try {
            VaultController::putShard(
                $this->db,
                $this->jctx('PUT', [
                    'vault_id' => self::VAULT_ID, 'folder_id' => self::FOLDER_A,
                ], $body),
            );
            self::fail('expected VaultShardTamperedError on edit-with-foreign-author');
        } catch (VaultShardTamperedError $e) {
            self::assertStringContainsString('author_device_id', $e->getMessage());
        }
    }

    public function test_putShard_409_shard_conflict_per_folder_scoped(): void
    {
        $this->seedShard(self::FOLDER_A, 1, 0, 'CURRENT');
        $body = [
            'expected_current_shard_revision' => 0,  // stale
            'new_shard_revision'              => 1,
            'parent_shard_revision'           => 0,
            'shard_hash'                      => str_repeat('a', 64),
            'shard_ciphertext'                => base64_encode($this->shardEnvelope(self::FOLDER_A, 1, 0)),
        ];
        try {
            VaultController::putShard(
                $this->db,
                $this->jctx('PUT', [
                    'vault_id' => self::VAULT_ID, 'folder_id' => self::FOLDER_A,
                ], $body),
            );
            self::fail('expected VaultShardConflictError');
        } catch (VaultShardConflictError $e) {
            self::assertSame('vault_shard_conflict', $e->errorCode);
            self::assertSame(self::FOLDER_A, $e->details['remote_folder_id']);
            self::assertSame(1, $e->details['current_shard_revision']);
        }
    }

    /**
     * Seed a current shard for the given folder via the repo so tests can
     * exercise the read endpoints without going through the atomic publish
     * path (which would also bump the root revision).
     */
    private function seedShard(string $folderId, int $rev, int $parent, string $cipher): void
    {
        $hash = hash('sha256', $cipher);
        (new VaultFolderShardsRepository($this->db))->tryCAS(
            self::VAULT_ID, $folderId, $parent, $rev, $parent, $hash, $cipher, strlen($cipher),
            self::DEVICE_ID, self::NOW + 1,
        );
    }

    // ===================================================================
    //  6.8 PUT /api/vaults/{id}/chunks/{chunk_id}
    // ===================================================================

    public function test_putChunk_happy_path_creates_blob_and_bumps_quota(): void
    {
        $chunkId = 'ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa';
        $bytes = str_repeat('x', 4096);

        $res = $this->invoke(fn() => VaultController::putChunk(
            $this->db, $this->ctx('PUT', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId], $bytes)
        ));

        self::assertSame(201, $res['status']);
        self::assertTrue($res['json']['data']['stored']);
        self::assertSame(strlen($bytes), $res['json']['data']['size']);

        $absPath = VaultStorage::chunkAbsolutePath(self::VAULT_ID, $chunkId);
        self::assertFileExists($absPath);
        self::assertSame($bytes, file_get_contents($absPath));

        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        self::assertSame(strlen($bytes), (int)$vault['used_ciphertext_bytes']);
        self::assertSame(1, (int)$vault['chunk_count']);
    }

    public function test_putChunk_idempotent_returns_200_no_quota_change(): void
    {
        $chunkId = 'ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb';
        $bytes = 'idempotent-chunk-bytes';
        $this->uploadChunk($chunkId, $bytes);

        $res = $this->invoke(fn() => VaultController::putChunk(
            $this->db, $this->ctx('PUT', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId], $bytes)
        ));
        self::assertSame(200, $res['status']);

        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        self::assertSame(strlen($bytes), (int)$vault['used_ciphertext_bytes']);
        self::assertSame(1, (int)$vault['chunk_count']);
    }

    public function test_putChunk_422_size_mismatch(): void
    {
        $chunkId = 'ch_v1_cccccccccccccccccccccccc';
        $this->uploadChunk($chunkId, 'first');

        try {
            VaultController::putChunk(
                $this->db,
                $this->ctx('PUT', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId], 'second-with-different-size')
            );
            self::fail('expected VaultChunkSizeMismatchError');
        } catch (VaultChunkSizeMismatchError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_chunk_size_mismatch', $e->errorCode);
            self::assertSame($chunkId, $e->details['chunk_id']);
        }
    }

    /**
     * Review §1.H6: when the post-commit disk write fails, the
     * row-delete + bytes-decrement must land atomically. Pre-fix
     * the two writes ran sequentially after the outer COMMIT; a
     * crash between them left ``used_ciphertext_bytes`` /
     * ``chunk_count`` skewed without the row to back the bytes.
     * Force the disk write to fail by pre-creating the chunk path
     * as a directory.
     */
    /**
     * Review §1.H6: source-level invariant for the rollback path.
     * deleteRow + incUsedBytes(-size, -1) must sit inside a writer
     * transaction (BEGIN IMMEDIATE / COMMIT). The end-to-end test
     * below verifies the no-crash outcome; this asserts the
     * transactional wrapping so a future refactor can't silently
     * un-bracket it.
     */
    public function test_putChunk_rollback_pair_is_wrapped_in_transaction(): void
    {
        $source = file_get_contents(__DIR__ . '/../../src/Controllers/VaultController.php');
        // Find the post-disk-failure rollback region and assert its
        // structure: BEGIN IMMEDIATE precedes BOTH writes, COMMIT
        // closes the block, and a catch arm runs ROLLBACK.
        $idx = strpos($source, 'Failed to write chunk to');
        self::assertNotFalse($idx, 'rollback region not found');
        // Walk backwards 1500 chars; the pair of writes + tx should
        // live in this window.
        $window = substr($source, max(0, $idx - 1500), 1500);
        self::assertStringContainsString("BEGIN IMMEDIATE", $window);
        self::assertStringContainsString("\$chunksRepo->deleteRow", $window);
        self::assertStringContainsString("\$vaultsRepo->incUsedBytes", $window);
        self::assertStringContainsString("COMMIT", $window);
        self::assertStringContainsString("ROLLBACK", $window);
        // The deleteRow + incUsedBytes calls must appear after the
        // BEGIN IMMEDIATE (so they're inside the transaction).
        $beginIdx = strrpos($window, "BEGIN IMMEDIATE");
        $deleteIdx = strrpos($window, "\$chunksRepo->deleteRow");
        $incIdx = strrpos($window, "\$vaultsRepo->incUsedBytes");
        $commitIdx = strrpos($window, "COMMIT");
        self::assertGreaterThan($beginIdx, $deleteIdx);
        self::assertGreaterThan($beginIdx, $incIdx);
        self::assertGreaterThan($incIdx, $commitIdx);
    }

    public function test_putChunk_atomically_rolls_back_on_disk_write_failure(): void
    {
        $chunkId = 'ch_v1_rollback234abcdefghijklm';
        // Force file_put_contents to fail: pre-create a directory at
        // the temp-write path's parent so the final rename target is
        // a directory (rename(file, dir) fails).
        $absPath = VaultStorage::chunkAbsolutePath(self::VAULT_ID, $chunkId);
        VaultStorage::ensureDir($absPath);
        mkdir($absPath, 0755, true);
        self::assertDirectoryExists($absPath);

        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        $usedBefore = (int)$vault['used_ciphertext_bytes'];
        $chunkCountBefore = (int)$vault['chunk_count'];

        try {
            VaultController::putChunk(
                $this->db,
                $this->ctx('PUT', [
                    'vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId,
                ], 'payload'),
            );
            self::fail('expected VaultStorageUnavailableError');
        } catch (VaultStorageUnavailableError $e) {
            self::assertSame(503, $e->status);
        }

        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        // No row left behind, AND the counters are exactly what they
        // were before the failed write — no half-state.
        $row = (new VaultChunksRepository($this->db))->get(self::VAULT_ID, $chunkId);
        self::assertNull(
            $row,
            'failed chunk write must clean up its row',
        );
        self::assertSame(
            $usedBefore, (int)$vault['used_ciphertext_bytes'],
            'used_ciphertext_bytes must return to pre-write value',
        );
        self::assertSame(
            $chunkCountBefore, (int)$vault['chunk_count'],
            'chunk_count must return to pre-write value',
        );
    }

    public function test_putChunk_400_on_invalid_chunk_id(): void
    {
        $this->expectException(VaultInvalidRequestError::class);
        VaultController::putChunk(
            $this->db,
            $this->ctx('PUT', [
                'vault_id' => self::VAULT_ID,
                'chunk_id' => 'ch_v2_aaaaaaaaaaaaaaaaaaaaaaaa',
            ], 'whatever')
        );
    }

    // ===================================================================
    //  Review §7.C2 — controller-level format-version gate
    // ===================================================================

    /**
     * Helper: flip the format-version byte at offset 0 to ``$badVersion``
     * so the resulting envelope passes the deterministic-prefix parser
     * but fails the ``guardFormatVersion`` check.
     */
    private function tamperFormatVersion(string $envelope, int $badVersion): string
    {
        $buf = $envelope;
        $buf[0] = chr($badVersion);
        return $buf;
    }

    public function test_putHeader_422_on_unsupported_format_version(): void
    {
        $badHeader = $this->tamperFormatVersion(
            $this->headerEnvelope(2), 0x02,
        );
        $body = [
            'expected_header_revision' => 1,
            'new_header_revision'      => 2,
            'encrypted_header'         => base64_encode($badHeader),
            'header_hash'              => str_repeat('a', 64),
        ];
        try {
            VaultController::putHeader(
                $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultFormatVersionUnsupportedError');
        } catch (VaultFormatVersionUnsupportedError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_format_version_unsupported', $e->errorCode);
        }
    }

    public function test_putRoot_422_on_unsupported_format_version(): void
    {
        $badRoot = $this->tamperFormatVersion(
            $this->rootEnvelope(2, 1), 0x02,
        );
        $body = [
            'expected_current_root_revision' => 1,
            'new_root_revision'              => 2,
            'parent_root_revision'           => 1,
            'root_hash'                      => str_repeat('1', 64),
            'root_ciphertext'                => base64_encode($badRoot),
        ];
        try {
            VaultController::putRoot(
                $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultFormatVersionUnsupportedError');
        } catch (VaultFormatVersionUnsupportedError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_format_version_unsupported', $e->errorCode);
        }
    }

    public function test_putShard_422_on_unsupported_format_version(): void
    {
        $badShard = $this->tamperFormatVersion(
            $this->shardEnvelope(self::FOLDER_A, 2, 1), 0x02,
        );
        $body = [
            'expected_current_shard_revision' => 1,
            'new_shard_revision'              => 2,
            'parent_shard_revision'           => 1,
            'shard_hash'                      => str_repeat('2', 64),
            'shard_ciphertext'                => base64_encode($badShard),
        ];
        try {
            VaultController::putShard(
                $this->db,
                $this->jctx('PUT', [
                    'vault_id'  => self::VAULT_ID,
                    'folder_id' => self::FOLDER_A,
                ], $body)
            );
            self::fail('expected VaultFormatVersionUnsupportedError');
        } catch (VaultFormatVersionUnsupportedError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_format_version_unsupported', $e->errorCode);
        }
    }

    public function test_putShardWithRoot_422_on_unsupported_root_format_version(): void
    {
        $badRoot = $this->tamperFormatVersion(
            $this->rootEnvelope(2, 1), 0x02,
        );
        $body = [
            'shard' => [
                'expected_current_shard_revision' => 1,
                'new_shard_revision'              => 2,
                'parent_shard_revision'           => 1,
                'shard_hash'                      => str_repeat('3', 64),
                'shard_ciphertext'                => base64_encode(
                    $this->shardEnvelope(self::FOLDER_A, 2, 1),
                ),
            ],
            'root' => [
                'expected_current_root_revision' => 1,
                'new_root_revision'              => 2,
                'parent_root_revision'           => 1,
                'root_hash'                      => str_repeat('4', 64),
                'root_ciphertext'                => base64_encode($badRoot),
            ],
        ];
        try {
            VaultController::putShardWithRoot(
                $this->db,
                $this->jctx('PUT', [
                    'vault_id'  => self::VAULT_ID,
                    'folder_id' => self::FOLDER_A,
                ], $body)
            );
            self::fail('expected VaultFormatVersionUnsupportedError');
        } catch (VaultFormatVersionUnsupportedError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_format_version_unsupported', $e->errorCode);
        }
    }

    /**
     * Regression for review §1.C1: putChunk against an existing row in
     * ``purged`` state must re-charge quota and revive the row, then
     * write the blob. Pre-fix head() returned the purged row, controller
     * skipped reserve, put() returned 'already_exists', controller 200'd
     * with no disk write.
     *
     * Simulates the race window after gc/execute committed but before
     * (or after a failed) unlink — i.e. the only durable scenario where
     * a row remains in ``purged`` after the §1.C2 reaper runs. (When
     * unlink succeeds the row is now deleted; that path goes through
     * the normal "insert fresh" branch of put() which the existing
     * happy-path test already covers.)
     */
    public function test_putChunk_against_purged_row_revives_blob(): void
    {
        $chunkId = 'ch_v1_repurge234abcdefghijklmn';
        $bytes = 're-upload-after-purge-bytes';

        $this->uploadChunk($chunkId, $bytes);
        $absPath = VaultStorage::chunkAbsolutePath(self::VAULT_ID, $chunkId);
        self::assertFileExists($absPath);

        // Simulate the post-gc/execute pre-unlink state directly: row
        // ``purged``, bytes already debited, file may or may not still
        // exist on disk. We unlink + decrement to mirror gc/execute's
        // committed-but-not-cleaned-up state, then call putChunk.
        $repo = new VaultChunksRepository($this->db);
        $repo->setState(self::VAULT_ID, $chunkId, VaultChunksRepository::STATE_PURGED);
        (new VaultsRepository($this->db))->incUsedBytes(self::VAULT_ID, -strlen($bytes), -1, self::NOW);
        @unlink($absPath);
        self::assertFileDoesNotExist($absPath);

        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        self::assertSame(0, (int)$vault['used_ciphertext_bytes']);
        self::assertSame(0, (int)$vault['chunk_count']);

        // Re-upload the same chunk: must restore disk + counters.
        $res = $this->invoke(fn() => VaultController::putChunk(
            $this->db,
            $this->ctx('PUT', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId], $bytes)
        ));
        self::assertSame(201, $res['status'], 'purged-row re-upload must be treated as fresh insert');
        self::assertTrue($res['json']['data']['stored']);

        self::assertFileExists($absPath);
        self::assertSame($bytes, file_get_contents($absPath));

        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        self::assertSame(strlen($bytes), (int)$vault['used_ciphertext_bytes']);
        self::assertSame(1, (int)$vault['chunk_count']);

        $revived = $repo->get(self::VAULT_ID, $chunkId);
        self::assertSame(VaultChunksRepository::STATE_ACTIVE, $revived['state']);

        // GET must serve the byte-identical blob (was 404 pre-fix).
        $getRes = $this->invoke(fn() => VaultController::getChunk(
            $this->db,
            $this->ctx('GET', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId])
        ));
        self::assertSame(200, $getRes['status']);
        self::assertSame($bytes, $getRes['raw']);
    }

    /**
     * Regression for review §1.C2: a previous gc/execute that committed
     * the state flip but failed to unlink (EBUSY / EIO / process crash
     * post-COMMIT) leaves an orphan blob on disk plus a stale ``purged``
     * row. The reaper invoked at the start of the next gc/execute must
     * retry the unlink and, on success, delete the row so it doesn't
     * leak forever — line 1206 of the historical gcExecute skipped
     * purged rows, so without the reaper nothing would ever clean them.
     */
    public function test_gcExecute_reaps_residual_purged_blobs(): void
    {
        $chunkId = 'ch_v1_residual2abcdefghijklmno';
        $bytes = 'orphan-residual';
        $this->uploadChunk($chunkId, $bytes);
        $absPath = VaultStorage::chunkAbsolutePath(self::VAULT_ID, $chunkId);
        self::assertFileExists($absPath);

        // Simulate "gc/execute flipped state and freed bytes but failed
        // to unlink"  — the F-S12 post-commit failure window.
        $repo = new VaultChunksRepository($this->db);
        $repo->setState(self::VAULT_ID, $chunkId, VaultChunksRepository::STATE_PURGED);
        (new VaultsRepository($this->db))->incUsedBytes(self::VAULT_ID, -strlen($bytes), -1, self::NOW);
        self::assertFileExists($absPath, 'unlink-failed scenario: blob still present');

        // Fire any gc/execute (empty plan suffices) — the reaper pass
        // at the top of the controller must clean the residual blob.
        $planRes = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'x',
                'candidate_chunk_ids' => [],
            ])
        ));
        $planId = $planRes['json']['data']['plan_id'];
        $this->invoke(fn() => VaultController::gcExecute(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['plan_id' => $planId])
        ));

        self::assertFileDoesNotExist($absPath, 'reaper must retry the unlink');
        self::assertNull(
            $repo->get(self::VAULT_ID, $chunkId),
            'reaper must remove the orphan row once the blob is gone'
        );
    }

    // ===================================================================
    //  6.9 GET /api/vaults/{id}/chunks/{chunk_id}
    // ===================================================================

    public function test_getChunk_happy_path_returns_binary(): void
    {
        $chunkId = 'ch_v1_dddddddddddddddddddddddd';
        $bytes = "binary-payload-with-\x00\xff-chars";
        $this->uploadChunk($chunkId, $bytes);

        $res = $this->invoke(fn() => VaultController::getChunk(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId])
        ));

        self::assertSame(200, $res['status']);
        self::assertSame($bytes, $res['raw']);
    }

    public function test_getChunk_404_vault_chunk_missing(): void
    {
        $this->expectException(VaultChunkMissingError::class);
        VaultController::getChunk($this->db, $this->ctx('GET', [
            'vault_id' => self::VAULT_ID,
            'chunk_id' => 'ch_v1_zzzzzzzzzzzzzzzzzzzzzzzz',
        ]));
    }

    /**
     * Review §1.H2: a chunk in state `purged` (or `gc_pending`) must
     * 404 on GET even if the on-disk file is briefly still present
     * — otherwise the GC window races against the client and the
     * relay serves content for a chunk it has already torn down.
     * headChunk already filters; this aligns getChunk.
     */
    public function test_getChunk_404_when_chunk_in_purged_state(): void
    {
        $chunkId = 'ch_v1_state2gateedabcdefghijkl';
        $this->uploadChunk($chunkId, b'still here');
        // Flip the row to purged without unlinking — emulates the
        // post-state-flip, pre-unlink window the §1.C2 reaper now
        // covers. Pre-§1.H2 the GET still returned 200 here.
        $repo = new VaultChunksRepository($this->db);
        $repo->setState(self::VAULT_ID, $chunkId, VaultChunksRepository::STATE_PURGED);

        $this->expectException(VaultChunkMissingError::class);
        VaultController::getChunk($this->db, $this->ctx('GET', [
            'vault_id' => self::VAULT_ID,
            'chunk_id' => $chunkId,
        ]));
    }

    // ===================================================================
    //  6.10 HEAD /api/vaults/{id}/chunks/{chunk_id}
    // ===================================================================

    public function test_headChunk_200_for_existing(): void
    {
        $chunkId = 'ch_v1_eeeeeeeeeeeeeeeeeeeeeeee';
        $this->uploadChunk($chunkId, 'head-test');

        $res = $this->invoke(fn() => VaultController::headChunk(
            $this->db, $this->ctx('HEAD', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId])
        ));

        self::assertSame(200, $res['status']);
        self::assertSame('', $res['raw']);
    }

    public function test_headChunk_404_for_missing(): void
    {
        $res = $this->invoke(fn() => VaultController::headChunk(
            $this->db, $this->ctx('HEAD', [
                'vault_id' => self::VAULT_ID,
                'chunk_id' => 'ch_v1_yyyyyyyyyyyyyyyyyyyyyyyy',
            ])
        ));

        self::assertSame(404, $res['status']);
        self::assertSame('', $res['raw']);
    }

    // ===================================================================
    //  6.11 POST /api/vaults/{id}/chunks/batch-head
    // ===================================================================

    public function test_batchHead_happy_path_present_and_missing(): void
    {
        $idA = 'ch_v1_ffffffffffffffffffffffff';
        $idB = 'ch_v1_gggggggggggggggggggggggg';
        $this->uploadChunk($idA, 'A');

        $res = $this->invoke(fn() => VaultController::batchHead(
            $this->db, $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['chunk_ids' => [$idA, $idB]])
        ));

        self::assertSame(200, $res['status']);
        self::assertTrue($res['json']['data']['chunks'][$idA]['present']);
        self::assertSame(1, $res['json']['data']['chunks'][$idA]['size']);
        self::assertFalse($res['json']['data']['chunks'][$idB]['present']);
    }

    public function test_batchHead_400_on_invalid_id(): void
    {
        $this->expectException(VaultInvalidRequestError::class);
        VaultController::batchHead($this->db, $this->jctx(
            'POST',
            ['vault_id' => self::VAULT_ID],
            ['chunk_ids' => ['ch_v1_' . str_repeat('a', 24), 'BAD-ID']]
        ));
    }

    // ===================================================================
    //  6.12 POST /api/vaults/{id}/gc/plan
    // ===================================================================

    public function test_gcPlan_happy_path_returns_safe_set(): void
    {
        $cid = 'ch_v1_hhhhhhhhhhhhhhhhhhhhhhhh';
        $this->uploadChunk($cid, 'plan-target');

        $res = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'opaque',
                'candidate_chunk_ids' => [$cid],
            ])
        ));

        self::assertSame(200, $res['status']);
        self::assertContains($cid, $res['json']['data']['safe_to_delete']);
        self::assertSame([], $res['json']['data']['still_referenced']);
        self::assertSame([], $res['json']['data']['already_deleted_chunk_ids']);
        self::assertNotEmpty($res['json']['data']['plan_id']);
    }

    public function test_gcPlan_returns_already_deleted_for_missing_chunks(): void
    {
        // Eviction crash-recovery: a prior run called gc_execute (chunks
        // deleted server-side) but crashed before publishing the shard
        // cleanup. The next run asks gc_plan about the same candidates
        // — the server reports them as already_deleted so the client
        // can clean stale shard entries without re-running gc_execute.
        $missing = 'ch_v1_aaaaaaaaaaaaaaaaaaaaalry';
        $active = 'ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb';
        $this->uploadChunk($active, 'still-here');

        $res = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'opaque',
                'candidate_chunk_ids' => [$missing, $active],
            ])
        ));

        self::assertSame(200, $res['status']);
        self::assertContains($active, $res['json']['data']['safe_to_delete']);
        self::assertContains($missing, $res['json']['data']['already_deleted_chunk_ids']);
        self::assertNotContains($missing, $res['json']['data']['safe_to_delete']);
    }

    public function test_gcPlan_400_on_unknown_root_revision(): void
    {
        $this->expectException(VaultInvalidRequestError::class);
        VaultController::gcPlan($this->db, $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
            'root_revision'       => 9999,
            'encrypted_gc_auth'   => 'opaque',
            'candidate_chunk_ids' => [],
        ]));
    }

    // ===================================================================
    //  6.13 POST /api/vaults/{id}/gc/execute
    // ===================================================================

    public function test_gcExecute_happy_path_purges_chunks(): void
    {
        $cid = 'ch_v1_iiiiiiiiiiiiiiiiiiiiiiii';
        $bytes = 'exec-target';
        $this->uploadChunk($cid, $bytes);

        $planRes = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'x',
                'candidate_chunk_ids' => [$cid],
            ])
        ));
        $planId = $planRes['json']['data']['plan_id'];

        $res = $this->invoke(fn() => VaultController::gcExecute(
            $this->db, $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['plan_id' => $planId])
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(1, $res['json']['data']['deleted_count']);
        self::assertSame(strlen($bytes), $res['json']['data']['freed_ciphertext_bytes']);

        self::assertFileDoesNotExist(VaultStorage::chunkAbsolutePath(self::VAULT_ID, $cid));
        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        self::assertSame(0, (int)$vault['used_ciphertext_bytes']);
    }

    public function test_gcExecute_404_for_unknown_plan(): void
    {
        $this->expectException(VaultNotFoundError::class);
        VaultController::gcExecute(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['plan_id' => 'pl_v1_doesnotexistdoesnotexist'])
        );
    }

    /**
     * Review §6.C5: getHeader must return ``caller_role`` so the
     * desktop can disable admin-only buttons (Schedule purge) when
     * the calling device isn't admin. /device-grants is admin-gated
     * so a sync-only device can't discover its own role through that
     * endpoint; getHeader is the natural carrier.
     */
    public function test_getHeader_includes_caller_role_for_admin(): void
    {
        $res = $this->invoke(fn() => VaultController::getHeader(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID])
        ));
        self::assertSame(200, $res['status']);
        self::assertSame('admin', $res['json']['data']['caller_role']);
    }

    public function test_getHeader_caller_role_reflects_demoted_role(): void
    {
        $this->demoteCaller('sync');
        $res = $this->invoke(fn() => VaultController::getHeader(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID])
        ));
        self::assertSame(200, $res['status']);
        self::assertSame('sync', $res['json']['data']['caller_role']);
    }

    /**
     * Review §3.C1: gcPlan with purpose='forced_eviction' must require
     * role=admin (the call is a hard-purge plan for eviction stages
     * 2/3). A sync-only device is forbidden from creating such a
     * plan — the corresponding eviction stages stay sync-driven only
     * for the safe stage-1 expired-tombstone case.
     */
    public function test_gcPlan_forced_eviction_forbidden_for_sync(): void
    {
        $this->demoteCaller('sync');
        try {
            VaultController::gcPlan(
                $this->db,
                $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                    'root_revision'       => 1,
                    'encrypted_gc_auth'   => 'x',
                    'candidate_chunk_ids' => [],
                    'purpose'             => 'forced_eviction',
                ])
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame(403, $e->status);
            self::assertSame('admin', $e->details['required_role']);
        }
    }

    /**
     * Review §3.C1: a sync-only device that somehow got hold of an
     * admin-issued forced_eviction plan id still fails at gc/execute.
     * Defense-in-depth: gcExecute looks up the job kind and enforces
     * admin on KIND_FORCED_EVICTION independently of the planner's
     * role check.
     */
    public function test_gcExecute_forced_eviction_forbidden_for_sync(): void
    {
        // Plan as admin (default seeded role), then demote and try to execute.
        $planRes = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'x',
                'candidate_chunk_ids' => [],
                'purpose'             => 'forced_eviction',
            ])
        ));
        $planId = $planRes['json']['data']['plan_id'];
        $this->demoteCaller('sync');
        try {
            VaultController::gcExecute(
                $this->db,
                $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['plan_id' => $planId])
            );
            self::fail('expected VaultPurgeNotAllowedError');
        } catch (VaultPurgeNotAllowedError $e) {
            self::assertSame(403, $e->status);
            self::assertStringContainsString('admin', $e->getMessage());
        }
    }

    /**
     * Review §1.M3 — a read-only caller must NOT be able to probe
     * gc/execute plan IDs and learn state from error codes. Pre-fix
     * gcExecute looked up the job + inspected its state BEFORE the
     * per-kind role check, so a read-only device could probe plans
     * by submitting plan_ids and watching the response codes
     * (completed→200, cancelled/expired→404, planned→400 about
     * state). Now ``requireRole(sync)`` runs upfront so the
     * read-only caller gets a uniform 403 before any plan lookup.
     */
    public function test_gcExecute_read_only_caller_403_before_plan_lookup(): void
    {
        // Plan a real GC job as the default (admin) caller.
        $cid = 'ch_v1_readonlyprobe234abcdefij';
        $bytes = 'probe-target';
        $this->uploadChunk($cid, $bytes);
        $planRes = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'x',
                'candidate_chunk_ids' => [$cid],
            ])
        ));
        $realPlanId = $planRes['json']['data']['plan_id'];

        // Demote caller to read-only. From here, gc/execute must 403
        // before the plan lookup runs — both a real plan_id and a
        // bogus one should return the same uniform "denied" error so
        // the attacker can't distinguish state.
        $this->demoteCaller('read-only');

        $bogusPlanId = 'pl_v1_doesnotexistdoesnotex';
        $shapes = [
            'real' => $realPlanId,
            'bogus' => $bogusPlanId,
        ];
        foreach ($shapes as $shape => $planId) {
            try {
                VaultController::gcExecute(
                    $this->db,
                    $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                        'plan_id' => $planId,
                    ])
                );
                self::fail("expected VaultAccessDeniedError for {$shape}");
            } catch (VaultAccessDeniedError $e) {
                self::assertSame(403, $e->status, "{$shape}: status");
                // Same error regardless of whether the plan_id is real.
                self::assertSame(
                    'sync', $e->details['required_role'] ?? null,
                    "{$shape}: required_role attribution",
                );
            }
        }
    }

    /**
     * Review §3.C1: admin device with purpose='forced_eviction'
     * succeeds end-to-end. Confirms the admin-gated path still works.
     */
    public function test_gcExecute_forced_eviction_admin_succeeds(): void
    {
        $cid = 'ch_v1_forcedevict234abcdefghij';
        $bytes = 'evict-me';
        $this->uploadChunk($cid, $bytes);

        $planRes = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'x',
                'candidate_chunk_ids' => [$cid],
                'purpose'             => 'forced_eviction',
            ])
        ));
        $planId = $planRes['json']['data']['plan_id'];

        $res = $this->invoke(fn() => VaultController::gcExecute(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['plan_id' => $planId])
        ));
        self::assertSame(200, $res['status']);
        self::assertSame(1, $res['json']['data']['deleted_count']);
    }

    // ===================================================================
    //  6.14 POST /api/vaults/{id}/gc/cancel
    // ===================================================================

    public function test_gcCancel_happy_path_marks_cancelled(): void
    {
        $planRes = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'x',
                'candidate_chunk_ids' => [],
            ])
        ));
        $planId = $planRes['json']['data']['plan_id'];

        $res = $this->invoke(fn() => VaultController::gcCancel(
            $this->db, $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['plan_id' => $planId])
        ));
        self::assertSame(204, $res['status']);
        self::assertSame('', $res['raw']);

        $row = (new VaultGcJobsRepository($this->db))->getById($planId);
        self::assertSame(VaultGcJobsRepository::STATE_CANCELLED, $row['state']);
    }

    /**
     * Review §1.H5: a sync-role device must not be able to cancel
     * another device's gc plan. Pre-fix the cancel ignored
     * ``requested_by_device_id`` entirely — a compromised paired
     * device could disrupt the legitimate admin's GC, eventually
     * exhausting quota.
     */
    public function test_gcCancel_forbids_cross_author_sync_caller(): void
    {
        // Plan as the seeded admin device.
        $planRes = $this->invoke(fn() => VaultController::gcPlan(
            $this->db, $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'root_revision'       => 1,
                'encrypted_gc_auth'   => 'x',
                'candidate_chunk_ids' => [],
            ])
        ));
        $planId = $planRes['json']['data']['plan_id'];

        // Demote to a sync role on the SAME device — still owner so
        // we have to switch caller to break the ownership check.
        // Easier: register a second device + grant it sync role.
        $secondDeviceId = '0102030405060708090a0b0c0d0e0f10';
        $secondToken = 'second-sync-token';
        (new DeviceRepository($this->db))->insertDevice(
            $secondDeviceId,
            base64_encode(random_bytes(32)),
            $secondToken,
            'desktop',
            self::NOW
        );
        (new VaultDeviceGrantsRepository($this->db))->insertGrant(
            'gr_v1_secondsync000000000000aa',
            self::VAULT_ID,
            $secondDeviceId,
            'Second Sync Desktop',
            'sync',
            self::DEVICE_ID,
            'recovery',
            self::NOW,
        );
        $_SERVER['HTTP_X_DEVICE_ID']   = $secondDeviceId;
        $_SERVER['HTTP_AUTHORIZATION'] = 'Bearer ' . $secondToken;

        try {
            VaultController::gcCancel(
                $this->db,
                $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['plan_id' => $planId])
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame(403, $e->status);
            self::assertSame('admin', $e->details['required_role']);
        }
    }

    public function test_gcCancel_idempotent_for_unknown_plan(): void
    {
        $res = $this->invoke(fn() => VaultController::gcCancel(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], ['plan_id' => 'pl_v1_doesnotexistdoesnotexist'])
        ));
        self::assertSame(204, $res['status']);
    }

    public function test_gcCancel_400_when_no_id_supplied(): void
    {
        $this->expectException(VaultInvalidRequestError::class);
        VaultController::gcCancel($this->db, $this->jctx('POST', ['vault_id' => self::VAULT_ID], []));
    }

    // ===================================================================
    //  Role enforcement (§D11) — read-only / browse-upload / sync caps
    // ===================================================================

    /**
     * Demote the seeded admin grant to ``$role`` (and replace any existing
     * grant for DEVICE_ID). Lets per-test cases pretend the caller is a
     * lesser-privileged device without spinning up a second device record.
     */
    private function demoteCaller(string $role): void
    {
        $this->db->execute(
            'UPDATE vault_device_grants SET role = :role
              WHERE vault_id = :vault AND device_id = :device',
            [
                ':role' => $role,
                ':vault' => self::VAULT_ID,
                ':device' => self::DEVICE_ID,
            ]
        );
    }

    public function test_putHeader_forbidden_for_non_admin(): void
    {
        $this->demoteCaller('sync');
        $body = [
            'expected_header_revision' => 1,
            'new_header_revision'      => 2,
            'encrypted_header'         => base64_encode($this->headerEnvelope(2)),
            'header_hash'              => str_repeat('a', 64),
        ];
        try {
            VaultController::putHeader(
                $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame(403, $e->status);
            self::assertSame('vault_access_denied', $e->errorCode);
            self::assertSame('admin', $e->details['required_role']);
        }
    }

    public function test_putRoot_forbidden_for_read_only(): void
    {
        $this->demoteCaller('read-only');
        $body = [
            'expected_current_root_revision' => 1,
            'new_root_revision'              => 2,
            'parent_root_revision'           => 1,
            'root_hash'                      => str_repeat('1', 64),
            'root_ciphertext'                => base64_encode($this->rootEnvelope(2, 1)),
        ];
        try {
            VaultController::putRoot(
                $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame('browse-upload', $e->details['required_role']);
        }
    }

    public function test_putChunk_forbidden_for_read_only(): void
    {
        $this->demoteCaller('read-only');
        try {
            VaultController::putChunk(
                $this->db,
                $this->ctx('PUT', [
                    'vault_id' => self::VAULT_ID,
                    'chunk_id' => 'ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa',
                ], 'small-bytes')
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame('browse-upload', $e->details['required_role']);
        }
    }

    public function test_putRoot_works_for_browse_upload_role(): void
    {
        $this->demoteCaller('browse-upload');
        $body = [
            'expected_current_root_revision' => 1,
            'new_root_revision'              => 2,
            'parent_root_revision'           => 1,
            'root_hash'                      => str_repeat('1', 64),
            'root_ciphertext'                => base64_encode($this->rootEnvelope(2, 1)),
        ];
        $res = $this->invoke(fn() => VaultController::putRoot(
            $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
        ));
        self::assertSame(200, $res['status']);
    }

    public function test_gcPlan_forbidden_for_browse_upload_role(): void
    {
        $this->demoteCaller('browse-upload');
        try {
            VaultController::gcPlan(
                $this->db,
                $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                    'root_revision'       => 1,
                    'candidate_chunk_ids' => [],
                ])
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame('sync', $e->details['required_role']);
        }
    }

    public function test_revoked_grant_blocks_writes(): void
    {
        $this->db->execute(
            'UPDATE vault_device_grants SET revoked_at = :now, revoked_by = :by
              WHERE vault_id = :vault AND device_id = :device',
            [
                ':now' => self::NOW + 1,
                ':by' => self::DEVICE_ID,
                ':vault' => self::VAULT_ID,
                ':device' => self::DEVICE_ID,
            ]
        );
        $body = [
            'expected_current_root_revision' => 1,
            'new_root_revision'              => 2,
            'parent_root_revision'           => 1,
            'root_hash'                      => str_repeat('1', 64),
            'root_ciphertext'                => base64_encode($this->rootEnvelope(2, 1)),
        ];
        try {
            VaultController::putRoot(
                $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultAccessDeniedError');
        } catch (VaultAccessDeniedError $e) {
            self::assertSame(403, $e->status);
            self::assertStringContainsString('revoked', $e->details['reason']);
        }
    }

    public function test_create_auto_inserts_admin_grant_for_creator(): void
    {
        $this->db->execute('DELETE FROM vault_root_manifests');
        $this->db->execute('DELETE FROM vault_folder_shards');
        $this->db->execute('DELETE FROM vault_folder_shard_heads');
        $this->db->execute('DELETE FROM vault_device_grants');
        $this->db->execute('DELETE FROM vaults');

        $body = [
            'vault_id'                => self::VAULT_ID_DASHED,
            'vault_access_token_hash' => base64_encode(hash('sha256', 'fresh-secret', true)),
            'encrypted_header'        => base64_encode($this->headerEnvelope(1)),
            'header_hash'             => self::HEADER_HASH,
            'initial_root_ciphertext' => base64_encode($this->rootEnvelope(1, 0)),
            'initial_root_hash'       => self::ROOT_HASH,
        ];
        $this->invoke(fn() => VaultController::create(
            $this->db, $this->jctx('POST', [], $body)
        ));

        $grant = (new VaultDeviceGrantsRepository($this->db))
            ->getByDevice(self::VAULT_ID, self::DEVICE_ID);
        self::assertNotNull($grant);
        self::assertSame('admin', (string)$grant['role']);
        self::assertSame('create', (string)$grant['granted_via']);
        self::assertMatchesRegularExpression('/^gr_v1_[a-z2-7]{24}$/', (string)$grant['grant_id']);
    }

    // ===================================================================
    //  F-T04 — migration endpoints: idempotency + commit semantics
    // ===================================================================

    public function test_migrationStart_same_device_retry_rotates_token(): void
    {
        $target = 'https://target.example.test';

        // First call: 201 + token_returned=true.
        $first = $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        self::assertSame(201, $first['status']);
        self::assertTrue($first['json']['data']['token_returned']);
        self::assertNotEmpty($first['json']['data']['token']);

        // Review §1.C3: a same-device retry recovers from a lost 201.
        // Status 200, token_returned=true with a freshly minted token,
        // started_at preserved. The previously-issued token is now
        // invalidated (only its hash is stored on the server).
        $second = $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        self::assertSame(200, $second['status']);
        self::assertTrue($second['json']['data']['token_returned']);
        self::assertNotEmpty($second['json']['data']['token']);
        self::assertNotSame(
            $first['json']['data']['token'],
            $second['json']['data']['token'],
            'lost-token recovery must hand back a fresh bearer',
        );
        self::assertSame(
            $first['json']['data']['started_at'],
            $second['json']['data']['started_at'],
            'started_at must be preserved across retried /start calls',
        );

        // Server now holds the hash of the second token. We can verify
        // by inspecting the persisted hash.
        $intent = (new VaultMigrationIntentsRepository($this->db))->getIntent(self::VAULT_ID);
        self::assertNotNull($intent);
        self::assertSame(
            hash('sha256', $second['json']['data']['token'], false),
            bin2hex((string)$intent['token_hash']),
        );
    }

    /**
     * Review §1.C3: a different admin device retrying /migration/start
     * observes the existing intent (metadata only) but must not get the
     * bearer token — that would silently transfer authorization away
     * from the originator. Same-target retries return token=null.
     */
    public function test_migrationStart_other_device_retry_returns_no_token(): void
    {
        $target = 'https://target.example.test';

        $first = $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        self::assertSame(201, $first['status']);
        self::assertNotEmpty($first['json']['data']['token']);

        // Register a second admin device + auth as it.
        $secondDeviceId = 'd9e8c7b6a5f4e3d2c1b0a9f8e7d6c5b4';
        $secondToken    = 'second-device-bearer';
        (new DeviceRepository($this->db))->insertDevice(
            $secondDeviceId,
            base64_encode(random_bytes(32)),
            $secondToken,
            'desktop',
            self::NOW
        );
        (new VaultDeviceGrantsRepository($this->db))->insertGrant(
            'gr_v1_secondadmin00000000aabb',
            self::VAULT_ID,
            $secondDeviceId,
            'Second Admin Desktop',
            'admin',
            self::DEVICE_ID,
            'recovery',
            self::NOW,
        );
        $_SERVER['HTTP_X_DEVICE_ID']   = $secondDeviceId;
        $_SERVER['HTTP_AUTHORIZATION'] = 'Bearer ' . $secondToken;

        $second = $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        self::assertSame(200, $second['status']);
        self::assertFalse($second['json']['data']['token_returned']);
        self::assertNull($second['json']['data']['token']);
        self::assertSame(
            $first['json']['data']['started_at'],
            $second['json']['data']['started_at'],
        );
    }

    /**
     * Review §1.H3: ``migrationStart`` previously accepted any
     * non-empty string; ``migrationCommit`` validated scheme +
     * filter_var. The asymmetry let a buggy admin client (or a
     * compromised paired-admin device) persist ``javascript:`` /
     * ``data:`` / ``file://`` targets at /start that the desktop's
     * switch-relay path might follow before /commit's check
     * fired. The validator now lives in a shared helper.
     */
    public function test_migrationStart_rejects_non_http_url(): void
    {
        try {
            VaultController::migrationStart(
                $this->db, $this->jctx(
                    'POST', ['vault_id' => self::VAULT_ID],
                    ['target_relay_url' => 'javascript:alert(1)'],
                ),
            );
            self::fail('expected VaultInvalidRequestError');
        } catch (VaultInvalidRequestError $e) {
            self::assertSame(400, $e->status);
            self::assertSame('target_relay_url', $e->details['field']);
        }
    }

    public function test_migrationStart_rejects_malformed_url(): void
    {
        try {
            VaultController::migrationStart(
                $this->db, $this->jctx(
                    'POST', ['vault_id' => self::VAULT_ID],
                    ['target_relay_url' => 'not a url'],
                ),
            );
            self::fail('expected VaultInvalidRequestError');
        } catch (VaultInvalidRequestError $e) {
            self::assertSame('target_relay_url', $e->details['field']);
        }
    }

    public function test_migrationStart_accepts_valid_https_url(): void
    {
        $res = $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => 'https://relay.example.test/path'],
            ),
        ));
        self::assertSame(201, $res['status']);
    }

    /**
     * Review §1.L2: by default ``migrationStart`` refuses loopback /
     * RFC 1918 / link-local hosts in the target URL so a malicious or
     * misconfigured admin can't redirect the paired-fleet at an
     * internal service. Operators who actually need a local URL (dev
     * rigs) opt in via ``migrationAllowPrivateUrls`` in
     * ``server/data/config.json``.
     */
    public function test_migrationStart_rejects_private_url_by_default(): void
    {
        $cases = [
            'http://localhost/path',
            'http://test.localhost',
            'http://127.0.0.1:4441',
            'http://10.0.0.1',
            'http://172.16.0.1',
            'http://192.168.1.1',
            'http://169.254.169.254',  // AWS / cloud link-local — defense in depth.
            'http://[::1]/path',
            'http://[fc00::1]/path',
        ];
        foreach ($cases as $url) {
            try {
                VaultController::migrationStart(
                    $this->db, $this->jctx(
                        'POST', ['vault_id' => self::VAULT_ID],
                        ['target_relay_url' => $url],
                    ),
                );
                self::fail("expected rejection for {$url}");
            } catch (VaultInvalidRequestError $e) {
                self::assertSame('target_relay_url', $e->details['field']);
            }
        }
    }

    public function test_migrationStart_different_target_409(): void
    {
        $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => 'https://target-a.example.test'],
            ),
        ));

        // Second call asking for a different target — vault can only
        // migrate to one place at a time per §H2.
        try {
            VaultController::migrationStart(
                $this->db, $this->jctx(
                    'POST', ['vault_id' => self::VAULT_ID],
                    ['target_relay_url' => 'https://target-b.example.test'],
                ),
            );
            self::fail('expected VaultMigrationInProgressError');
        } catch (VaultMigrationInProgressError $exc) {
            self::assertSame(409, $exc->status);
            self::assertSame('vault_migration_in_progress', $exc->errorCode);
        }
    }

    public function test_migrationCommit_marks_read_only(): void
    {
        $target = 'https://target.example.test';
        // Start.
        $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        // Verify (sets verified_at; commit gates on it).
        $this->invoke(fn() => VaultController::migrationVerifySource(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID]),
        ));
        // Commit.
        $res = $this->invoke(fn() => VaultController::migrationCommit(
            $this->db, $this->jctx(
                'PUT', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        self::assertSame(200, $res['status']);

        // Vault row now flagged read-only via migrated_to.
        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        self::assertSame($target, (string)$vault['migrated_to']);
    }

    public function test_getHeader_after_commit_carries_migrated_to(): void
    {
        // Review §7.H5: PHP twin of the desktop-side discovery test
        // (``test_propagate_relay_migration_*``). After /migration/commit
        // the source relay's GET /header MUST return migrated_to so
        // other devices (B...N) learn of the migration on their next
        // header fetch. Pre-existing PHP test only asserted the row's
        // ``migrated_to`` column was set — it never verified the field
        // surfaces on the public endpoint. A future refactor that
        // dropped the field from the JSON response shape would leave
        // every secondary device silently stranded on the old URL.
        $target = 'https://target.example.test';

        // Pre-commit baseline: getHeader returns migrated_to=null.
        $before = $this->invoke(fn() => VaultController::getHeader(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID]),
        ));
        self::assertSame(200, $before['status']);
        self::assertNull($before['json']['data']['migrated_to']);

        // Drive the full /start → /verify → /commit sequence.
        $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        $this->invoke(fn() => VaultController::migrationVerifySource(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID]),
        ));
        $this->invoke(fn() => VaultController::migrationCommit(
            $this->db, $this->jctx(
                'PUT', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));

        // Post-commit: getHeader now carries migrated_to=<target>. This
        // is the discovery signal the desktop's propagate_relay_migration
        // consumes — without it B...N never switch over.
        $after = $this->invoke(fn() => VaultController::getHeader(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID]),
        ));
        self::assertSame(200, $after['status']);
        self::assertSame(
            $target, $after['json']['data']['migrated_to'],
            'GET /header must surface migrated_to post-commit so other '
            . 'devices learn of the migration on their next header fetch'
        );
        // Other header fields stay intact — the migration flip doesn't
        // disturb the data plane until the consumer routes to the new URL.
        self::assertSame(
            $before['json']['data']['vault_id'],
            $after['json']['data']['vault_id'],
        );
        self::assertSame(
            $before['json']['data']['header_revision'],
            $after['json']['data']['header_revision'],
        );
        self::assertNotEmpty($after['json']['data']['encrypted_header']);
    }

    public function test_migrationVerifySource_post_commit_does_not_stamp_new_verified_at(): void
    {
        // Review §1.M7 — verify-source after /commit must NOT acquire a
        // brand-new verified_at. Verification is a pre-commit primitive;
        // the field should only ever record when the pre-commit state
        // was attested. Pre-fix the behaviour was correct by accident
        // (COALESCE preserved any pre-commit timestamp), but a vault
        // whose verified_at was reset to NULL post-commit (or never
        // stamped because /commit ran via some other code path) would
        // silently acquire a post-commit verified_at on the next
        // /verify-source call.
        $target = 'https://target.example.test';
        $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        // Verify once + commit — happy path.
        $this->invoke(fn() => VaultController::migrationVerifySource(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID]),
        ));
        $this->invoke(fn() => VaultController::migrationCommit(
            $this->db, $this->jctx(
                'PUT', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));

        // Simulate the pathological state: post-commit with
        // verified_at scrubbed (e.g. a future code path that resets
        // the field, or a manual operator intervention). The vault's
        // migrated_to is set; verified_at is NULL.
        $this->db->execute(
            'UPDATE vault_migration_intents SET verified_at = NULL WHERE vault_id = :id',
            [':id' => self::VAULT_ID],
        );

        // Call /verify-source again — the explicit state guard must
        // refuse to stamp a new timestamp because the vault is
        // already migrated_to. The endpoint still succeeds (it's
        // deliberately read-only after commit so other devices can
        // verify the pre-commit state didn't drift), but the response
        // must reflect that verified_at was NOT freshly stamped.
        $after = $this->invoke(fn() => VaultController::migrationVerifySource(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID]),
        ));
        self::assertSame(200, $after['status']);
        // verified_at in the DB row should still be NULL.
        $intent = (new VaultMigrationIntentsRepository($this->db))->getIntent(
            self::VAULT_ID,
        );
        self::assertNull(
            $intent['verified_at'],
            'verify-source after /commit must NOT stamp verified_at',
        );
    }

    public function test_migrationCommit_repeat_returns_same_committed_at(): void
    {
        $target = 'https://target.example.test';
        $this->invoke(fn() => VaultController::migrationStart(
            $this->db, $this->jctx(
                'POST', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        $this->invoke(fn() => VaultController::migrationVerifySource(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID]),
        ));
        $first = $this->invoke(fn() => VaultController::migrationCommit(
            $this->db, $this->jctx(
                'PUT', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        // Second commit with same target: idempotent. F-S05 contract
        // says committed_at is preserved across retries (the COALESCE
        // in markCommitted), so the second response carries the same
        // timestamp as the first.
        $second = $this->invoke(fn() => VaultController::migrationCommit(
            $this->db, $this->jctx(
                'PUT', ['vault_id' => self::VAULT_ID],
                ['target_relay_url' => $target],
            ),
        ));
        self::assertSame(200, $second['status']);
        self::assertSame(
            $first['json']['data']['committed_at'],
            $second['json']['data']['committed_at'],
            'committed_at must be preserved across retried /commit calls',
        );
    }
}
