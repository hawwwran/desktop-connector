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
    private const MFST_HASH       = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
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
            self::MFST_HASH,
            self::NOW
        );
        (new VaultManifestsRepository($this->db))->create(
            self::VAULT_ID,
            1,
            0,
            self::MFST_HASH,
            "\x02manifest",
            8,
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
     */
    private function manifestEnvelope(
        int $revision,
        int $parentRevision,
        string $authorDeviceId = self::DEVICE_ID,
        string $vaultId = self::VAULT_ID,
        string $aeadCiphertextAndTag = "stub-ciphertext"
    ): string {
        return VaultCrypto::buildManifestEnvelope(
            $vaultId,
            $revision,
            $parentRevision,
            $authorDeviceId,
            str_repeat("\0", 24),
            $aeadCiphertextAndTag,
        );
    }

    /** Build a syntactically-correct header envelope (formats §9.1). */
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
                    self::chunkBody($bytes),
                )
            );
        } finally {
            ob_end_clean();
        }
    }

    /** Prepend the v1 format-version byte (formats §11.3) to a chunk body. */
    private static function chunkBody(string $bytes): string
    {
        if ($bytes !== '' && $bytes[0] === "\x01") {
            return $bytes;
        }
        return "\x01" . $bytes;
    }

    // ===================================================================
    //  6.1 POST /api/vaults
    // ===================================================================

    public function test_create_happy_path(): void
    {
        $this->db->execute('DELETE FROM vaults');
        $this->db->execute('DELETE FROM vault_manifests');

        $body = [
            'vault_id'                    => self::VAULT_ID_DASHED,
            'vault_access_token_hash'     => base64_encode(hash('sha256', 'fresh-secret', true)),
            'encrypted_header'            => base64_encode("\x01\x02header-bytes"),
            'header_hash'                 => self::HEADER_HASH,
            'initial_manifest_ciphertext' => base64_encode("\x03\x04manifest"),
            'initial_manifest_hash'       => self::MFST_HASH,
        ];

        $res = $this->invoke(fn() => VaultController::create(
            $this->db, $this->jctx('POST', [], $body)
        ));

        self::assertSame(201, $res['status']);
        self::assertTrue($res['json']['ok']);
        self::assertSame(self::VAULT_ID_DASHED, $res['json']['data']['vault_id']);
        self::assertSame(1, $res['json']['data']['header_revision']);
        self::assertSame(1073741824, $res['json']['data']['quota_ciphertext_bytes']);
        self::assertSame(0, $res['json']['data']['used_ciphertext_bytes']);
    }

    public function test_create_returns_409_vault_already_exists(): void
    {
        $body = [
            'vault_id'                    => self::VAULT_ID_DASHED,
            'vault_access_token_hash'     => base64_encode(hash('sha256', 'x', true)),
            'encrypted_header'            => base64_encode('h'),
            'header_hash'                 => self::HEADER_HASH,
            'initial_manifest_ciphertext' => base64_encode('m'),
            'initial_manifest_hash'       => self::MFST_HASH,
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

    public function test_create_400_on_invalid_vault_id(): void
    {
        $body = [
            'vault_id'                    => 'not-a-vault-id',
            'vault_access_token_hash'     => base64_encode(hash('sha256', 'x', true)),
            'encrypted_header'            => base64_encode('h'),
            'header_hash'                 => self::HEADER_HASH,
            'initial_manifest_ciphertext' => base64_encode('m'),
            'initial_manifest_hash'       => self::MFST_HASH,
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

    // ===================================================================
    //  6.4 GET /api/vaults/{id}/manifest
    // ===================================================================

    public function test_getManifest_happy_path(): void
    {
        $res = $this->invoke(fn() => VaultController::getManifest(
            $this->db, $this->ctx('GET', ['vault_id' => self::VAULT_ID])
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(1, $res['json']['data']['revision']);
        self::assertSame(0, $res['json']['data']['parent_revision']);
        self::assertSame(self::MFST_HASH, $res['json']['data']['manifest_hash']);
        self::assertSame(base64_encode("\x02manifest"), $res['json']['data']['manifest_ciphertext']);
        self::assertSame(8, $res['json']['data']['manifest_size']);
    }

    public function test_getManifest_404_unknown_vault(): void
    {
        $this->expectException(VaultNotFoundError::class);
        VaultController::getManifest($this->db, $this->ctx('GET', ['vault_id' => 'AAAAAAAAAAAA']));
    }

    // ===================================================================
    //  6.6 PUT /api/vaults/{id}/manifest (CAS, A1 conflict)
    // ===================================================================

    public function test_putManifest_happy_path_advances_head(): void
    {
        $newHash = str_repeat('1', 64);
        $body = [
            'expected_current_revision' => 1,
            'new_revision'              => 2,
            'parent_revision'           => 1,
            'manifest_hash'             => $newHash,
            'manifest_ciphertext'       => base64_encode($this->manifestEnvelope(2, 1)),
        ];

        $res = $this->invoke(fn() => VaultController::putManifest(
            $this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
        ));

        self::assertSame(200, $res['status']);
        self::assertSame(2, $res['json']['data']['revision']);
        self::assertSame($newHash, $res['json']['data']['manifest_hash']);
    }

    public function test_putManifest_400_when_envelope_revision_disagrees(): void
    {
        // Body says revision=2 / parent=1; envelope says revision=99 / parent=98.
        $body = [
            'expected_current_revision' => 1,
            'new_revision'              => 2,
            'parent_revision'           => 1,
            'manifest_hash'             => str_repeat('1', 64),
            'manifest_ciphertext'       => base64_encode($this->manifestEnvelope(99, 98)),
        ];
        try {
            VaultController::putManifest(
                $this->db,
                $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultManifestTamperedError');
        } catch (VaultManifestTamperedError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_manifest_tampered', $e->errorCode);
        }
    }

    public function test_putManifest_400_when_envelope_author_disagrees(): void
    {
        // Envelope's author_device_id differs from the X-Device-ID header.
        $body = [
            'expected_current_revision' => 1,
            'new_revision'              => 2,
            'parent_revision'           => 1,
            'manifest_hash'             => str_repeat('1', 64),
            'manifest_ciphertext'       => base64_encode($this->manifestEnvelope(
                2, 1, str_repeat('f', 32)
            )),
        ];
        try {
            VaultController::putManifest(
                $this->db,
                $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body)
            );
            self::fail('expected VaultManifestTamperedError');
        } catch (VaultManifestTamperedError $e) {
            self::assertSame(422, $e->status);
            self::assertStringContainsString('author_device_id', $e->details['reason']);
        }
    }

    public function test_putManifest_409_a1_conflict_payload(): void
    {
        $body = [
            'expected_current_revision' => 99,
            'new_revision'              => 100,
            'parent_revision'           => 99,
            'manifest_hash'             => str_repeat('2', 64),
            'manifest_ciphertext'       => base64_encode($this->manifestEnvelope(100, 99)),
        ];

        try {
            VaultController::putManifest($this->db, $this->jctx('PUT', ['vault_id' => self::VAULT_ID], $body));
            self::fail('expected VaultManifestConflictError with A1 payload');
        } catch (VaultManifestConflictError $e) {
            self::assertSame('vault_manifest_conflict', $e->errorCode);
            self::assertSame(1, $e->details['current_revision']);
            self::assertSame(99, $e->details['expected_revision']);
            self::assertSame(self::MFST_HASH, $e->details['current_manifest_hash']);
            self::assertSame(base64_encode("\x02manifest"), $e->details['current_manifest_ciphertext']);
            self::assertSame(8, $e->details['current_manifest_size']);
        }
    }

    // ===================================================================
    //  6.8 PUT /api/vaults/{id}/chunks/{chunk_id}
    // ===================================================================

    public function test_putChunk_happy_path_creates_blob_and_bumps_quota(): void
    {
        $chunkId = 'ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa';
        $bytes = self::chunkBody(str_repeat('x', 4096));

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
            $this->db, $this->ctx('PUT', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId], self::chunkBody($bytes))
        ));
        self::assertSame(200, $res['status']);

        $vault = (new VaultsRepository($this->db))->getById(self::VAULT_ID);
        self::assertSame(strlen(self::chunkBody($bytes)), (int)$vault['used_ciphertext_bytes']);
        self::assertSame(1, (int)$vault['chunk_count']);
    }

    public function test_putChunk_422_size_mismatch(): void
    {
        $chunkId = 'ch_v1_cccccccccccccccccccccccc';
        $this->uploadChunk($chunkId, 'first');

        try {
            VaultController::putChunk(
                $this->db,
                $this->ctx('PUT', ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId], self::chunkBody('second-with-different-size'))
            );
            self::fail('expected VaultChunkSizeMismatchError');
        } catch (VaultChunkSizeMismatchError $e) {
            self::assertSame(422, $e->status);
            self::assertSame('vault_chunk_size_mismatch', $e->errorCode);
            self::assertSame($chunkId, $e->details['chunk_id']);
        }
    }

    public function test_putChunk_400_on_invalid_chunk_id(): void
    {
        $this->expectException(VaultInvalidRequestError::class);
        VaultController::putChunk(
            $this->db,
            $this->ctx('PUT', [
                'vault_id' => self::VAULT_ID,
                'chunk_id' => 'ch_v2_aaaaaaaaaaaaaaaaaaaaaaaa',
            ], self::chunkBody('whatever'))
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
        self::assertSame(self::chunkBody($bytes), $res['raw']);
    }

    public function test_getChunk_404_vault_chunk_missing(): void
    {
        $this->expectException(VaultChunkMissingError::class);
        VaultController::getChunk($this->db, $this->ctx('GET', [
            'vault_id' => self::VAULT_ID,
            'chunk_id' => 'ch_v1_zzzzzzzzzzzzzzzzzzzzzzzz',
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
        // Stored body has the +1 prefix byte.
        self::assertSame(strlen(self::chunkBody('A')), $res['json']['data']['chunks'][$idA]['size']);
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
                'manifest_revision'   => 1,
                'encrypted_gc_auth'   => 'opaque',
                'candidate_chunk_ids' => [$cid],
            ])
        ));

        self::assertSame(200, $res['status']);
        self::assertContains($cid, $res['json']['data']['safe_to_delete']);
        self::assertSame([], $res['json']['data']['still_referenced']);
        self::assertNotEmpty($res['json']['data']['plan_id']);
    }

    public function test_gcPlan_400_on_unknown_manifest_revision(): void
    {
        $this->expectException(VaultInvalidRequestError::class);
        VaultController::gcPlan($this->db, $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
            'manifest_revision'   => 9999,
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
                'manifest_revision'   => 1,
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
        self::assertSame(strlen(self::chunkBody($bytes)), $res['json']['data']['freed_ciphertext_bytes']);

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

    // ===================================================================
    //  6.14 POST /api/vaults/{id}/gc/cancel
    // ===================================================================

    public function test_gcCancel_happy_path_marks_cancelled(): void
    {
        $planRes = $this->invoke(fn() => VaultController::gcPlan(
            $this->db,
            $this->jctx('POST', ['vault_id' => self::VAULT_ID], [
                'manifest_revision'   => 1,
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

    public function test_putManifest_forbidden_for_read_only(): void
    {
        $this->demoteCaller('read-only');
        $body = [
            'expected_current_revision' => 1,
            'new_revision'              => 2,
            'parent_revision'           => 1,
            'manifest_hash'             => str_repeat('1', 64),
            'manifest_ciphertext'       => base64_encode($this->manifestEnvelope(2, 1)),
        ];
        try {
            VaultController::putManifest(
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

    public function test_putManifest_works_for_browse_upload_role(): void
    {
        $this->demoteCaller('browse-upload');
        $body = [
            'expected_current_revision' => 1,
            'new_revision'              => 2,
            'parent_revision'           => 1,
            'manifest_hash'             => str_repeat('1', 64),
            'manifest_ciphertext'       => base64_encode($this->manifestEnvelope(2, 1)),
        ];
        $res = $this->invoke(fn() => VaultController::putManifest(
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
                    'manifest_revision'   => 1,
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
            'expected_current_revision' => 1,
            'new_revision'              => 2,
            'parent_revision'           => 1,
            'manifest_hash'             => str_repeat('1', 64),
            'manifest_ciphertext'       => base64_encode($this->manifestEnvelope(2, 1)),
        ];
        try {
            VaultController::putManifest(
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
        $this->db->execute('DELETE FROM vault_manifests');
        $this->db->execute('DELETE FROM vault_device_grants');
        $this->db->execute('DELETE FROM vaults');

        $body = [
            'vault_id'                    => self::VAULT_ID_DASHED,
            'vault_access_token_hash'     => base64_encode(hash('sha256', 'fresh-secret', true)),
            'encrypted_header'            => base64_encode('header'),
            'header_hash'                 => self::HEADER_HASH,
            'initial_manifest_ciphertext' => base64_encode('manifest'),
            'initial_manifest_hash'       => self::MFST_HASH,
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
}
