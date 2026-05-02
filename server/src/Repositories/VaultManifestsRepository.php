<?php

/**
 * Owns the immutable manifest-revision chain in `vault_manifests` and the
 * head pointer that lives on `vaults.current_manifest_revision` /
 * `vaults.current_manifest_hash`.
 *
 * The hot path here is `tryCAS()`: it advances the head and inserts a new
 * revision row atomically. Two non-obvious constraints live here:
 *
 *   - The conditional UPDATE on `vaults` is the CAS primitive. Splitting
 *     into SELECT-then-UPDATE opens the same race the existing
 *     PingRateRepository::tryClaimCooldown closes — don't refactor that
 *     way without a replacement primitive.
 *
 *   - On CAS failure the repo returns the §A1 conflict payload (current
 *     revision + hash + ciphertext + size). Per T0 §A1 the client never
 *     has to issue a follow-up GET /manifest after a 409 — controllers
 *     forward this payload verbatim into the error envelope's `details`.
 */
class VaultManifestsRepository
{
    public function __construct(private Database $db) {}

    /**
     * Insert any manifest revision, including the genesis (parent_revision = 0).
     * Caller is responsible for keeping `vaults.current_manifest_revision`
     * in sync — the genesis insert pairs with a fresh `VaultsRepository::create()`
     * that already wrote `current_manifest_revision = 1`. Subsequent
     * publishes use `tryCAS()` instead so the two writes stay atomic.
     */
    public function create(
        string $vaultId,
        int $revision,
        int $parentRevision,
        string $manifestHash,
        string $manifestCiphertext,
        int $manifestSize,
        string $authorDeviceId,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO vault_manifests (
                vault_id,
                revision,
                parent_revision,
                manifest_hash,
                manifest_ciphertext,
                manifest_size,
                author_device_id,
                created_at
             ) VALUES (
                :vault_id,
                :revision,
                :parent_revision,
                :hash,
                :ciphertext,
                :size,
                :author,
                :now
             )',
            [
                ':vault_id'        => $vaultId,
                ':revision'        => $revision,
                ':parent_revision' => $parentRevision,
                ':hash'            => $manifestHash,
                ':ciphertext'      => $manifestCiphertext,
                ':size'            => $manifestSize,
                ':author'          => $authorDeviceId,
                ':now'             => $now,
            ]
        );
    }

    /**
     * The current head: the manifest row whose `revision` matches
     * `vaults.current_manifest_revision`. Returns null if the vault is
     * unknown or has no head row yet (the latter shouldn't happen in
     * practice — genesis is written eagerly with the vault).
     */
    public function getCurrent(string $vaultId): ?array
    {
        return $this->db->querySingle(
            'SELECT vm.vault_id,
                    vm.revision,
                    vm.parent_revision,
                    vm.manifest_hash,
                    vm.manifest_ciphertext,
                    vm.manifest_size,
                    vm.author_device_id,
                    vm.created_at
             FROM vault_manifests vm
             INNER JOIN vaults v
                 ON v.vault_id = vm.vault_id
                AND v.current_manifest_revision = vm.revision
             WHERE vm.vault_id = :id',
            [':id' => $vaultId]
        );
    }

    /**
     * A specific historical revision. Used by the activity timeline
     * (T17.1) and by "restore folder to date" (T11.5). Returns null on
     * unknown (vault_id, revision).
     */
    public function getByRevision(string $vaultId, int $revision): ?array
    {
        return $this->db->querySingle(
            'SELECT vault_id,
                    revision,
                    parent_revision,
                    manifest_hash,
                    manifest_ciphertext,
                    manifest_size,
                    author_device_id,
                    created_at
             FROM vault_manifests
             WHERE vault_id = :id
               AND revision = :rev',
            [':id' => $vaultId, ':rev' => $revision]
        );
    }

    /**
     * CAS publish. Atomically:
     *
     *   1. UPDATE vaults SET current_manifest_revision = :new, current_manifest_hash = :hash
     *      WHERE vault_id = :id AND current_manifest_revision = :expected
     *   2. INSERT INTO vault_manifests (...)
     *
     * Returns null on success. On conflict (the WHERE in step 1 didn't
     * match because someone else already advanced the head) returns the
     * full T0 §A1 payload — caller forwards it verbatim into the 409
     * vault_manifest_conflict response so the client can run the §D4
     * merge without an extra round-trip.
     *
     * Both writes happen inside a single SQLite IMMEDIATE transaction so
     * a failure between them rolls back cleanly.
     *
     * @return array{
     *     current_revision: int,
     *     current_manifest_hash: string,
     *     current_manifest_ciphertext: string,
     *     current_manifest_size: int
     * }|null
     */
    public function tryCAS(
        string $vaultId,
        int $expectedCurrentRevision,
        int $newRevision,
        string $manifestHash,
        string $manifestCiphertext,
        int $manifestSize,
        string $authorDeviceId,
        int $now
    ): ?array {
        $this->db->execute('BEGIN IMMEDIATE');
        try {
            $this->db->execute(
                'UPDATE vaults
                 SET current_manifest_revision = :new,
                     current_manifest_hash     = :hash,
                     updated_at                = :now
                 WHERE vault_id                  = :id
                   AND current_manifest_revision = :expected',
                [
                    ':new'      => $newRevision,
                    ':hash'     => $manifestHash,
                    ':now'      => $now,
                    ':id'       => $vaultId,
                    ':expected' => $expectedCurrentRevision,
                ]
            );
            if ($this->db->changes() !== 1) {
                $this->db->execute('ROLLBACK');
                return $this->fetchConflictPayload($vaultId);
            }

            $this->create(
                $vaultId,
                $newRevision,
                $expectedCurrentRevision,
                $manifestHash,
                $manifestCiphertext,
                $manifestSize,
                $authorDeviceId,
                $now
            );

            $this->db->execute('COMMIT');
            return null;
        } catch (\Throwable $e) {
            $this->db->execute('ROLLBACK');
            throw $e;
        }
    }

    /**
     * Read the current manifest into the §A1 conflict shape. Called when
     * tryCAS sees a stale `expected_current_revision` so the caller can
     * forward this payload into the 409 error body and the client can
     * re-merge in one round-trip.
     *
     * Returns an empty array if the vault is unknown — controllers should
     * have already validated existence via `requireVaultAuth` before
     * calling tryCAS, so this is a defensive fallback only.
     */
    private function fetchConflictPayload(string $vaultId): array
    {
        $row = $this->db->querySingle(
            'SELECT vm.revision        AS current_revision,
                    vm.manifest_hash    AS current_manifest_hash,
                    vm.manifest_ciphertext AS current_manifest_ciphertext,
                    vm.manifest_size    AS current_manifest_size
             FROM vault_manifests vm
             INNER JOIN vaults v
                 ON v.vault_id = vm.vault_id
                AND v.current_manifest_revision = vm.revision
             WHERE vm.vault_id = :id',
            [':id' => $vaultId]
        );
        if ($row === null) {
            return [
                'current_revision'            => 0,
                'current_manifest_hash'       => '',
                'current_manifest_ciphertext' => '',
                'current_manifest_size'       => 0,
            ];
        }
        return [
            'current_revision'            => (int)$row['current_revision'],
            'current_manifest_hash'       => (string)$row['current_manifest_hash'],
            'current_manifest_ciphertext' => (string)$row['current_manifest_ciphertext'],
            'current_manifest_size'       => (int)$row['current_manifest_size'],
        ];
    }
}
