<?php

declare(strict_types=1);

use PHPUnit\Framework\Attributes\RunTestsInSeparateProcesses;
use PHPUnit\Framework\TestCase;

/**
 * Tests for the vault capability advertising logic (T1.7).
 *
 * Acceptance:
 *   - Partially-implemented build (one T1 endpoint missing) does NOT
 *     advertise the aggregate `vault_v1`.
 *   - Transfer-only relay (no vault endpoints) advertises no vault bits
 *     at all (regression).
 */
#[RunTestsInSeparateProcesses]
final class VaultCapabilitiesTest extends TestCase
{
    protected function tearDown(): void
    {
        VaultCapabilities::clearOverride();
    }

    public function test_full_t1_surface_advertises_aggregate_and_all_sub_bits(): void
    {
        VaultCapabilities::clearOverride();
        $bits = VaultCapabilities::current();

        self::assertContains('vault_v1', $bits);
        self::assertContains('vault_create_v1', $bits);
        self::assertContains('vault_header_v1', $bits);
        self::assertContains('vault_manifest_cas_v1', $bits);
        self::assertContains('vault_chunk_v1', $bits);
        self::assertContains('vault_gc_v1', $bits);
    }

    public function test_partial_implementation_drops_aggregate_but_keeps_sub_bits(): void
    {
        // Acceptance criterion: one T1 endpoint missing → no `vault_v1`.
        VaultCapabilities::setDisabled(['vault_chunk_v1']);
        $bits = VaultCapabilities::current();

        self::assertNotContains('vault_v1', $bits);
        self::assertNotContains('vault_chunk_v1', $bits);

        // The other four T1 sub-bits are still advertised so a client
        // that only needs them can gate on the finer bit.
        self::assertContains('vault_create_v1', $bits);
        self::assertContains('vault_header_v1', $bits);
        self::assertContains('vault_manifest_cas_v1', $bits);
        self::assertContains('vault_gc_v1', $bits);
    }

    public function test_disable_each_t1_bit_independently_drops_aggregate(): void
    {
        // Property-style: every individual T1 bit is load-bearing for the
        // aggregate. Walk through each, disable one at a time, assert the
        // aggregate is gone.
        $allT1 = [
            'vault_create_v1',
            'vault_header_v1',
            'vault_manifest_cas_v1',
            'vault_chunk_v1',
            'vault_gc_v1',
        ];
        foreach ($allT1 as $bit) {
            VaultCapabilities::setDisabled([$bit]);
            $current = VaultCapabilities::current();
            self::assertNotContains(
                'vault_v1',
                $current,
                "aggregate must be off when {$bit} is missing"
            );
            self::assertNotContains($bit, $current);
        }
    }

    public function test_transfer_only_relay_advertises_no_vault_bits(): void
    {
        // Regression: a deployment that intentionally hasn't wired any
        // vault endpoints (transfer/fasttrack only) emits zero vault bits.
        // We model this by disabling every advertised bit; the production
        // equivalent is a relay whose code lacks the vault routes entirely
        // — same observable output.
        VaultCapabilities::setDisabled([
            'vault_create_v1',
            'vault_header_v1',
            'vault_manifest_cas_v1',
            'vault_chunk_v1',
            'vault_gc_v1',
            'vault_soft_delete_v1',
            'vault_export_v1',
            'vault_migration_v1',
            'vault_grant_qr_v1',
            'vault_purge_v1',
        ]);

        $bits = VaultCapabilities::current();
        self::assertSame([], $bits);
    }

    public function test_post_t1_bits_are_advertised(): void
    {
        // T7 / T8 / T9 / T13 / T14 have all landed; clients gate
        // per-feature on their respective bit so they're listed
        // alongside the T1 sub-bits. The aggregate `vault_v1` is
        // unrelated (T1-only).
        VaultCapabilities::clearOverride();
        $bits = VaultCapabilities::current();

        self::assertContains('vault_soft_delete_v1', $bits);
        self::assertContains('vault_export_v1', $bits);
        self::assertContains('vault_migration_v1', $bits);
        self::assertContains('vault_grant_qr_v1', $bits);
        self::assertContains('vault_purge_v1', $bits);
    }

    public function test_clearOverride_restores_default_state(): void
    {
        VaultCapabilities::setDisabled(['vault_chunk_v1']);
        self::assertNotContains('vault_v1', VaultCapabilities::current());

        VaultCapabilities::clearOverride();
        self::assertContains('vault_v1', VaultCapabilities::current());
    }

    public function test_aggregate_appears_first_in_list(): void
    {
        VaultCapabilities::clearOverride();
        $bits = VaultCapabilities::current();

        // The wire spec is unordered, but stable order makes diffs in
        // fixtures (and human reading of the JSON) cleaner.
        self::assertSame('vault_v1', $bits[0]);
    }
}
