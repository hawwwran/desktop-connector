<?php

/**
 * Builds the vault portion of the `/api/health.capabilities` list per
 * T0 §D12. Two-layer logic:
 *
 *   - Per-endpoint bits (`vault_create_v1`, `vault_header_v1`, …) are
 *     advertised when the corresponding controller is wired in this
 *     build. Future bits land here as later phases add them.
 *
 *   - The aggregate `vault_v1` bit flips on **only** when every T1
 *     mandatory sub-bit is present (per D12: `vault_create_v1`,
 *     `vault_header_v1`, `vault_manifest_cas_v1`, `vault_chunk_v1`,
 *     `vault_gc_v1`). Clients gating on `vault_v1` get a complete v1
 *     surface; clients gating on a finer bit get the corresponding
 *     feature without depending on later phases.
 *
 * Why a class instead of a hardcoded array: tests need to verify the
 * "partial implementation drops the aggregate" rule (T1.7 acceptance)
 * AND the "transfer-only relay advertises no vault bits" regression.
 * `setDisabled()` and `clearOverride()` give tests a switch without
 * faking missing files at the autoload layer.
 */
class VaultCapabilities
{
    /**
     * T1 bits — when all five are present, `vault_v1` is added on top.
     * Order in this array is also the order they appear in the response
     * (clients match by membership, but stable order keeps fixtures
     * deterministic).
     */
    private const T1_BITS = [
        'vault_create_v1',
        'vault_header_v1',
        'vault_manifest_cas_v1',
        'vault_chunk_v1',
        'vault_gc_v1',
    ];

    /**
     * Bits that don't gate the aggregate but should appear in the list
     * when their backing phase has landed. Order matches the spec
     * (`docs/protocol/vault-v1.md` §1) for stable wire output.
     */
    private const POST_T1_BITS = [
        'vault_soft_delete_v1', // T7
        'vault_export_v1',      // T8
        'vault_migration_v1',   // T9
        'vault_grant_qr_v1',    // T13
        'vault_purge_v1',       // T14
    ];

    /**
     * Test-only override. Lists bits that are "missing" — VaultCapabilities
     * pretends the controller for that bit isn't wired. Production never
     * sets this. Tests use `setDisabled([...])` to flip individual bits.
     */
    private static array $disabled = [];

    /** Mark these capability bits as not present. Used by tests. */
    public static function setDisabled(array $bits): void
    {
        self::$disabled = array_values($bits);
    }

    /** Restore the production default (every bit advertised). */
    public static function clearOverride(): void
    {
        self::$disabled = [];
    }

    /**
     * The bits to merge into `/api/health.capabilities`. Always returns
     * the same shape: an array of strings, possibly empty.
     */
    public static function current(): array
    {
        $bits = [];
        $t1Present = 0;
        foreach (self::T1_BITS as $bit) {
            if (!in_array($bit, self::$disabled, true)) {
                $bits[] = $bit;
                $t1Present++;
            }
        }
        foreach (self::POST_T1_BITS as $bit) {
            if (!in_array($bit, self::$disabled, true)) {
                $bits[] = $bit;
            }
        }
        // Aggregate only when ALL T1 bits are present.
        if ($t1Present === count(self::T1_BITS)) {
            array_unshift($bits, 'vault_v1');
        }
        return $bits;
    }
}
