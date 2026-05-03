<?php

declare(strict_types=1);

use PHPUnit\Framework\Attributes\RunTestsInSeparateProcesses;
use PHPUnit\Framework\TestCase;

/**
 * Integration test for the wiring between VaultCapabilities and the
 * /api/health endpoint (T1.7). Confirms the health response actually
 * carries the vault bits we advertise.
 *
 * Doesn't go over real HTTP — DeviceController::health is invoked
 * directly with output buffering, mirroring the VaultControllerTest
 * pattern.
 */
#[RunTestsInSeparateProcesses]
final class HealthCapabilitiesIntegrationTest extends TestCase
{
    private string $dbPath;
    private Database $db;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'vault_health_test_');
        $this->db     = Database::fromPath($this->dbPath);
        $this->db->migrate();
    }

    protected function tearDown(): void
    {
        VaultCapabilities::clearOverride();
        @unlink($this->dbPath);
        @unlink($this->dbPath . '-shm');
        @unlink($this->dbPath . '-wal');
    }

    private function invoke(callable $fn): array
    {
        ob_start();
        try {
            $fn();
        } finally {
            $raw = ob_get_clean();
        }
        return json_decode($raw, true) ?? [];
    }

    public function test_health_advertises_vault_v1_when_full_t1_present(): void
    {
        VaultCapabilities::clearOverride();

        $resp = $this->invoke(fn() => DeviceController::health(
            $this->db, new RequestContext(method: 'GET')
        ));

        self::assertSame('ok', $resp['status']);
        self::assertContains('vault_v1', $resp['capabilities']);
        self::assertContains('vault_chunk_v1', $resp['capabilities']);
    }

    public function test_health_drops_vault_v1_on_partial_implementation(): void
    {
        VaultCapabilities::setDisabled(['vault_gc_v1']);

        $resp = $this->invoke(fn() => DeviceController::health(
            $this->db, new RequestContext(method: 'GET')
        ));

        self::assertNotContains('vault_v1', $resp['capabilities']);
        self::assertNotContains('vault_gc_v1', $resp['capabilities']);
        self::assertContains('vault_chunk_v1', $resp['capabilities']);
    }

    public function test_health_emits_no_vault_bits_for_transfer_only_relay(): void
    {
        // Simulate a relay that hasn't wired any vault endpoints by
        // disabling the entire T1 surface.
        VaultCapabilities::setDisabled([
            'vault_create_v1',
            'vault_header_v1',
            'vault_manifest_cas_v1',
            'vault_chunk_v1',
            'vault_gc_v1',
        ]);

        $resp = $this->invoke(fn() => DeviceController::health(
            $this->db, new RequestContext(method: 'GET')
        ));

        $vaultBits = array_filter(
            $resp['capabilities'],
            fn(string $b) => str_starts_with($b, 'vault_')
        );
        self::assertSame([], array_values($vaultBits));
    }

    public function test_health_preserves_existing_stream_v1_capability(): void
    {
        // Regression: adding vault bits doesn't drop transfer-pipeline bits.
        VaultCapabilities::clearOverride();

        $resp = $this->invoke(fn() => DeviceController::health(
            $this->db, new RequestContext(method: 'GET')
        ));

        // stream_v1 is on by default unless Config flips streamingEnabled off.
        self::assertContains('stream_v1', $resp['capabilities']);
    }
}
