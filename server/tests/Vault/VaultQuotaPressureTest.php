<?php

declare(strict_types=1);

use PHPUnit\Framework\Attributes\RunTestsInSeparateProcesses;
use PHPUnit\Framework\TestCase;

/**
 * T1.8: quota-pressure tracking.
 *
 * Walk a vault from 0 → 80% → 90% → 100% of its quota by uploading chunks
 * and verifying that GET /api/vaults/{id}/header reports the right
 * used_ciphertext_bytes / quota_ciphertext_bytes pair on every read. The
 * desktop client's UX bands (no warning < 80%, soft warning ≥ 80%, prominent
 * ≥ 90%, full at 100%) are computed client-side from these two numbers, so
 * the contract here is "the numbers update on every chunk write".
 *
 * The default quota is 1 GiB per T0 §D2; tests override to a small number
 * via direct UPDATE so the band walk is practical without 800 MB chunks.
 */
#[RunTestsInSeparateProcesses]
final class VaultQuotaPressureTest extends TestCase
{
    private string $dbPath;
    private string $storageRoot;
    private Database $db;

    private const VAULT_ID        = 'ABCD2345WXYZ';
    private const VAULT_ID_DASHED = 'ABCD-2345-WXYZ';
    private const VAULT_SECRET    = 'pressure-test-secret';
    private const HEADER_HASH     = 'aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344';
    private const MFST_HASH       = 'eeff00112233445566778899aabbccdd00112233445566778899aabbccddeeff';
    private const NOW             = 1714680000;
    private const DEVICE_ID       = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
    private const DEVICE_TOKEN    = 'device-bearer-token';

    /** Tight quota so the band walk is hand-counted byte-exact. */
    private const TEST_QUOTA = 1000;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_quota_test_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();

        $this->storageRoot = sys_get_temp_dir() . '/vault_quota_storage_' . bin2hex(random_bytes(4));
        mkdir($this->storageRoot, 0700, true);
        VaultStorage::setRoot($this->storageRoot);

        (new DeviceRepository($this->db))->insertDevice(
            self::DEVICE_ID, base64_encode(random_bytes(32)),
            self::DEVICE_TOKEN, 'desktop', self::NOW
        );
        (new VaultsRepository($this->db))->create(
            self::VAULT_ID, hash('sha256', self::VAULT_SECRET, true),
            "\x01header", self::HEADER_HASH, self::MFST_HASH, self::NOW
        );
        (new VaultManifestsRepository($this->db))->create(
            self::VAULT_ID, 1, 0, self::MFST_HASH, "\x02manifest", 8,
            self::DEVICE_ID, self::NOW
        );

        // §D11 role gate — the test seeds the vault directly, so add the
        // matching admin grant by hand (production goes through
        // VaultController::create which auto-inserts it).
        (new VaultDeviceGrantsRepository($this->db))->insertGrant(
            'gr_v1_quotaadmin0000000000000a',
            self::VAULT_ID, self::DEVICE_ID, 'Quota Test Admin', 'admin',
            self::DEVICE_ID, 'create', self::NOW,
        );

        // Tighten the quota for this test only.
        $this->db->execute(
            'UPDATE vaults SET quota_ciphertext_bytes = :q WHERE vault_id = :id',
            [':q' => self::TEST_QUOTA, ':id' => self::VAULT_ID]
        );

        $_SERVER['HTTP_X_DEVICE_ID']           = self::DEVICE_ID;
        $_SERVER['HTTP_AUTHORIZATION']         = 'Bearer ' . self::DEVICE_TOKEN;
        $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] = 'Bearer ' . self::VAULT_SECRET;
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
            'HTTP_X_VAULT_AUTHORIZATION', 'HTTP_X_VAULT_ID',
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

    /** Quietly upload a chunk and discard the controller's echoed JSON. */
    private function uploadChunk(string $chunkId, string $bytes): void
    {
        // Prepend the v1 format-version byte (formats §11.3) so the
        // server's plaintext-byte gate accepts the body.
        $body = ($bytes !== '' && $bytes[0] === "\x01") ? $bytes : "\x01" . $bytes;
        ob_start();
        try {
            VaultController::putChunk(
                $this->db,
                new RequestContext(
                    method: 'PUT',
                    params: ['vault_id' => self::VAULT_ID, 'chunk_id' => $chunkId],
                    bodyOverride: $body
                )
            );
        } finally {
            ob_end_clean();
        }
    }

    /** Read the header and return the {used, quota} pair. */
    private function readHeader(): array
    {
        ob_start();
        try {
            VaultController::getHeader(
                $this->db,
                new RequestContext(method: 'GET', params: ['vault_id' => self::VAULT_ID])
            );
            $raw = ob_get_clean();
        } catch (\Throwable $e) {
            ob_end_clean();
            throw $e;
        }
        $json = json_decode($raw, true);
        return [
            'used'  => (int)$json['data']['used_ciphertext_bytes'],
            'quota' => (int)$json['data']['quota_ciphertext_bytes'],
        ];
    }

    public function test_header_includes_quota_and_used_pair_on_fresh_vault(): void
    {
        $h = $this->readHeader();
        self::assertSame(0, $h['used']);
        self::assertSame(self::TEST_QUOTA, $h['quota']);
    }

    public function test_threshold_walk_80_90_100_percent(): void
    {
        // Each chunk gets a +1 byte format-version prefix on the wire,
        // so the on-disk size is `caller_bytes + 1`. Reflect that in
        // the assertions to keep the hand-counted walk explicit.

        // 1. Upload 799 bytes (+1 prefix = 800) — vault at 80%.
        $this->uploadChunk('ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa', str_repeat('a', 799));
        $at80 = $this->readHeader();
        self::assertSame(800, $at80['used']);
        self::assertSame(self::TEST_QUOTA, $at80['quota']);
        self::assertSame(80.0, ($at80['used'] / $at80['quota']) * 100.0);

        // 2. Upload 99 bytes (+1 prefix = 100) — vault at 90%.
        $this->uploadChunk('ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb', str_repeat('b', 99));
        $at90 = $this->readHeader();
        self::assertSame(900, $at90['used']);
        self::assertSame(90.0, ($at90['used'] / $at90['quota']) * 100.0);

        // 3. Upload 99 bytes (+1 prefix = 100) — vault at 100%.
        $this->uploadChunk('ch_v1_cccccccccccccccccccccccc', str_repeat('c', 99));
        $at100 = $this->readHeader();
        self::assertSame(1000, $at100['used']);
        self::assertSame(100.0, ($at100['used'] / $at100['quota']) * 100.0);
    }

    public function test_upload_past_100_percent_returns_507(): void
    {
        // Fill the vault. The +1 format-version prefix is added by
        // uploadChunk, so caller bytes = TEST_QUOTA - 1.
        $this->uploadChunk('ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa', str_repeat('a', self::TEST_QUOTA - 1));

        // Next byte should fail with vault_quota_exceeded.
        try {
            $this->uploadChunk('ch_v1_bbbbbbbbbbbbbbbbbbbbbbbb', 'x');
            self::fail('expected VaultQuotaExceededError when over quota');
        } catch (VaultQuotaExceededError $e) {
            self::assertSame(507, $e->status);
            self::assertSame('vault_quota_exceeded', $e->errorCode);
            self::assertSame(self::TEST_QUOTA, $e->details['quota_bytes']);
            self::assertFalse($e->details['eviction_available']);
        }

        $h = $this->readHeader();
        self::assertSame(self::TEST_QUOTA, $h['used']);
    }

    public function test_idempotent_reupload_does_not_double_count(): void
    {
        // T0 §A21: quota counts unique chunks. Re-uploading the same chunk
        // is a no-op and must not bump used_ciphertext_bytes a second time.
        $this->uploadChunk('ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa', str_repeat('a', 499));
        $first = $this->readHeader();

        $this->uploadChunk('ch_v1_aaaaaaaaaaaaaaaaaaaaaaaa', str_repeat('a', 499));
        $second = $this->readHeader();

        self::assertSame(500, $first['used']);
        self::assertSame(500, $second['used']);
    }

    public function test_dashed_vault_id_in_response(): void
    {
        // Ergonomic fixture: clients reading the header want the dashed
        // form (display-friendly). Confirms VaultController::getHeader's
        // dashedVaultId() formatter is applied to the response field.
        ob_start();
        VaultController::getHeader(
            $this->db,
            new RequestContext(method: 'GET', params: ['vault_id' => self::VAULT_ID])
        );
        $resp = json_decode(ob_get_clean(), true);
        self::assertSame(self::VAULT_ID_DASHED, $resp['data']['vault_id']);
    }
}
