<?php

/**
 * Owns all SQL touching the `vaults` row — vault metadata, quota counters,
 * header CAS state, and H2 migration state. Services and controllers express
 * intent ("create a vault", "stamp this vault as migrated"); this repo holds
 * the queries and the column-name assumptions per server/migrations/002_vault.sql.
 *
 * Two non-obvious invariants live here:
 *
 *   - `header_revision` is bumped via CAS on every header write
 *     (setHeaderCiphertext). The conditional UPDATE's WHERE clause is what
 *     makes concurrent header writes safe under SQLite's WAL serialization;
 *     do not split into SELECT-then-UPDATE.
 *
 *   - `migrated_to IS NOT NULL` means the vault is read-only on this relay
 *     (T0 §H2 source-side rule). The repo exposes that signal via
 *     isReadOnly(); enforcement happens at the request-handler layer.
 */
class VaultsRepository
{
    public function __construct(private Database $db) {}

    /**
     * Insert a brand-new vault with its initial header + manifest. Caller
     * is responsible for ensuring vault_id uniqueness; SQLite's PRIMARY KEY
     * constraint will reject collisions (controllers translate to 409
     * vault_already_exists per T0 error table).
     *
     * @param string $vaultAccessTokenHash Raw bytes of SHA-256(vault_access_secret).
     * @param string $encryptedHeader      Header envelope per formats §9.
     * @param string $headerHash           Hex sha-256 of the header envelope.
     * @param int    $now                  Unix epoch seconds.
     */
    public function create(
        string $vaultId,
        string $vaultAccessTokenHash,
        string $encryptedHeader,
        string $headerHash,
        string $initialManifestHash,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO vaults (
                vault_id,
                vault_access_token_hash,
                encrypted_header,
                header_revision,
                header_hash,
                current_manifest_revision,
                current_manifest_hash,
                used_ciphertext_bytes,
                chunk_count,
                created_at,
                updated_at
             ) VALUES (
                :vault_id,
                :token_hash,
                :enc_header,
                1,
                :header_hash,
                1,
                :manifest_hash,
                0,
                0,
                :now,
                :now
             )',
            [
                ':vault_id'      => $vaultId,
                ':token_hash'    => new Blob($vaultAccessTokenHash),
                ':enc_header'    => new Blob($encryptedHeader),
                ':header_hash'   => $headerHash,
                ':manifest_hash' => $initialManifestHash,
                ':now'           => $now,
            ]
        );
    }

    /**
     * Returns the vault row keyed by id, or null when no vault exists.
     * Callers rely on the null check; do not return [] for absent rows.
     */
    public function getById(string $vaultId): ?array
    {
        return $this->db->querySingle(
            'SELECT vault_id, vault_access_token_hash, encrypted_header,
                    header_revision, header_hash,
                    current_manifest_revision, current_manifest_hash,
                    used_ciphertext_bytes, chunk_count, quota_ciphertext_bytes,
                    purge_token_hash,
                    migrated_to, migrated_at, previous_relay_url,
                    soft_deleted_at, pending_purge_at,
                    created_at, updated_at
             FROM vaults
             WHERE vault_id = :id',
            [':id' => $vaultId]
        );
    }

    /**
     * Header-only fetch for GET /api/vaults/{id}/header. Returned shape
     * matches what the controller serializes onto the wire (vault-v1.md §6.2):
     * encrypted_header, header_hash, header_revision, plus the quota counters
     * that drive the 80/90/100 % pressure bands client-side.
     */
    public function getHeaderCiphertext(string $vaultId): ?array
    {
        return $this->db->querySingle(
            'SELECT encrypted_header, header_hash, header_revision,
                    quota_ciphertext_bytes, used_ciphertext_bytes,
                    migrated_to, previous_relay_url
             FROM vaults
             WHERE vault_id = :id',
            [':id' => $vaultId]
        );
    }

    /**
     * CAS-protected header update. Returns true when the update landed,
     * false when the expected revision didn't match (controllers translate
     * to 409 vault_manifest_conflict). The UPDATE's revision filter is the
     * primitive that closes the race; do not lift it client-side.
     */
    public function setHeaderCiphertext(
        string $vaultId,
        string $encryptedHeader,
        string $headerHash,
        int $expectedHeaderRevision,
        int $now
    ): bool {
        $this->db->execute(
            'UPDATE vaults
             SET encrypted_header = :enc_header,
                 header_hash      = :header_hash,
                 header_revision  = header_revision + 1,
                 updated_at       = :now
             WHERE vault_id        = :id
               AND header_revision = :expected',
            [
                ':enc_header'  => new Blob($encryptedHeader),
                ':header_hash' => $headerHash,
                ':now'         => $now,
                ':id'          => $vaultId,
                ':expected'    => $expectedHeaderRevision,
            ]
        );
        return $this->db->changes() === 1;
    }

    /**
     * Adjust the global ciphertext byte counter and chunk count atomically.
     * Use $byteDelta > 0 / $chunkDelta > 0 on chunk store; negative on GC.
     * Per T0 §A21, this counter tracks unique chunks across the whole vault
     * (per-folder usage is descriptive, computed client-side from the
     * decrypted manifest).
     */
    public function incUsedBytes(
        string $vaultId,
        int $byteDelta,
        int $chunkDelta,
        int $now
    ): void {
        $this->db->execute(
            'UPDATE vaults
             SET used_ciphertext_bytes = used_ciphertext_bytes + :bytes,
                 chunk_count           = chunk_count + :chunks,
                 updated_at            = :now
             WHERE vault_id = :id',
            [
                ':bytes'  => $byteDelta,
                ':chunks' => $chunkDelta,
                ':now'    => $now,
                ':id'     => $vaultId,
            ]
        );
    }

    /**
     * `quota_ciphertext_bytes - used_ciphertext_bytes`. Returns null if the
     * vault is unknown. Negative values are possible only via storage
     * accounting bugs and are not corrected here — callers can clamp to 0.
     */
    public function getQuotaRemaining(string $vaultId): ?int
    {
        $row = $this->db->querySingle(
            'SELECT quota_ciphertext_bytes - used_ciphertext_bytes AS remaining
             FROM vaults
             WHERE vault_id = :id',
            [':id' => $vaultId]
        );
        return $row === null ? null : (int)$row['remaining'];
    }

    /**
     * Stamp the vault as migrated to a new relay (T0 §H2). After this
     * returns true, the vault is read-only on this relay — every subsequent
     * write should be rejected with 409 vault_migration_in_progress
     * (state="committed"). Idempotent: a repeat call with the same target
     * URL no-ops; calling with a different URL while migrated returns false
     * (the caller surfaces 409 with the existing target).
     */
    public function markMigratedTo(
        string $vaultId,
        string $targetRelayUrl,
        int $now
    ): bool {
        $this->db->execute(
            'UPDATE vaults
             SET migrated_to = :target,
                 migrated_at = :now,
                 updated_at  = :now
             WHERE vault_id = :id
               AND (migrated_to IS NULL OR migrated_to = :target)',
            [
                ':target' => $targetRelayUrl,
                ':now'    => $now,
                ':id'     => $vaultId,
            ]
        );
        return $this->db->changes() === 1;
    }

    /**
     * Clear the migrated_to / migrated_at fields (used by the post-commit
     * 7-day rollback path or by abandon-and-rollback during the verify
     * phase). Returns true when a row was actually flipped; false when the
     * vault wasn't in a migrated state.
     */
    public function cancelMigration(string $vaultId, int $now): bool
    {
        $this->db->execute(
            'UPDATE vaults
             SET migrated_to = NULL,
                 migrated_at = NULL,
                 updated_at  = :now
             WHERE vault_id    = :id
               AND migrated_to IS NOT NULL',
            [':now' => $now, ':id' => $vaultId]
        );
        return $this->db->changes() === 1;
    }

    /**
     * True iff this relay should refuse writes to the vault. Currently this
     * means "post-migration on the source side" (migrated_to IS NOT NULL)
     * OR "tombstoned at the relay level" (soft_deleted_at IS NOT NULL).
     * Pending hard-purge (pending_purge_at) does NOT make the vault read-only;
     * the purge runs at scheduled_for, before which writes are still legal.
     */
    public function isReadOnly(string $vaultId): bool
    {
        $row = $this->db->querySingle(
            'SELECT 1 AS ro
             FROM vaults
             WHERE vault_id = :id
               AND (migrated_to IS NOT NULL OR soft_deleted_at IS NOT NULL)',
            [':id' => $vaultId]
        );
        return $row !== null;
    }
}
