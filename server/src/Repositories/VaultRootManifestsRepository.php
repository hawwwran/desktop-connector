<?php

/**
 * Owns the immutable root-manifest revision chain in `vault_root_manifests`
 * and the head pointer that lives on `vaults.current_root_revision` /
 * `vaults.current_root_hash`. Replaces the legacy
 * ``VaultManifestsRepository`` after the Phase B sharding migration.
 *
 * The hot path is ``tryCAS()``: it advances the head and inserts a new
 * revision row atomically. Same two non-obvious constraints as the
 * legacy repo:
 *
 *   - The conditional UPDATE on `vaults` is the CAS primitive. Splitting
 *     into SELECT-then-UPDATE re-opens a class of TOCTOU races that
 *     ``PingRateRepository::tryClaimCooldown`` closes the same way.
 *
 *   - On CAS failure the repo returns the §A1-root conflict payload
 *     (current root revision + hash + ciphertext + size). Per
 *     `vault-v1.md` §6.6 the client never needs a follow-up
 *     ``GET /root`` after a 409 — controllers forward the payload
 *     verbatim into the error envelope's `details`.
 */
class VaultRootManifestsRepository
{
    public function __construct(private Database $db) {}

    /**
     * Insert any root revision, including the genesis
     * (`parent_root_revision = 0`). Caller keeps
     * `vaults.current_root_revision` in sync — the genesis insert pairs
     * with a fresh ``VaultsRepository::create()`` that wrote
     * ``current_root_revision = 1``. Subsequent publishes use
     * ``tryCAS()`` so the two writes stay atomic.
     */
    public function create(
        string $vaultId,
        int $rootRevision,
        int $parentRootRevision,
        string $rootHash,
        string $rootCiphertext,
        int $rootSize,
        string $authorDeviceId,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO vault_root_manifests (
                vault_id,
                root_revision,
                parent_root_revision,
                root_hash,
                root_ciphertext,
                root_size,
                author_device_id,
                created_at
             ) VALUES (
                :vault_id,
                :root_revision,
                :parent_root_revision,
                :hash,
                :ciphertext,
                :size,
                :author,
                :now
             )',
            [
                ':vault_id'             => $vaultId,
                ':root_revision'        => $rootRevision,
                ':parent_root_revision' => $parentRootRevision,
                ':hash'                 => $rootHash,
                ':ciphertext'           => new Blob($rootCiphertext),
                ':size'                 => $rootSize,
                ':author'               => $authorDeviceId,
                ':now'                  => $now,
            ]
        );
    }

    /**
     * The current head: the root row whose `root_revision` matches
     * `vaults.current_root_revision`. Returns null if the vault is
     * unknown or has no head row yet (genesis is written eagerly with
     * the vault).
     */
    public function getCurrent(string $vaultId): ?array
    {
        return $this->db->querySingle(
            'SELECT vrm.vault_id,
                    vrm.root_revision,
                    vrm.parent_root_revision,
                    vrm.root_hash,
                    vrm.root_ciphertext,
                    vrm.root_size,
                    vrm.author_device_id,
                    vrm.created_at
             FROM vault_root_manifests vrm
             INNER JOIN vaults v
                 ON v.vault_id = vrm.vault_id
                AND v.current_root_revision = vrm.root_revision
             WHERE vrm.vault_id = :id',
            [':id' => $vaultId]
        );
    }

    /**
     * A specific historical root revision. Used by the activity
     * timeline (T17.1) and "restore folder to date" (T11.5). Returns
     * null on unknown (vault_id, root_revision).
     */
    public function getByRevision(string $vaultId, int $rootRevision): ?array
    {
        return $this->db->querySingle(
            'SELECT vault_id,
                    root_revision,
                    parent_root_revision,
                    root_hash,
                    root_ciphertext,
                    root_size,
                    author_device_id,
                    created_at
             FROM vault_root_manifests
             WHERE vault_id = :id
               AND root_revision = :rev',
            [':id' => $vaultId, ':rev' => $rootRevision]
        );
    }

    /**
     * CAS publish. Atomically:
     *
     *   1. UPDATE vaults SET current_root_revision = :new, current_root_hash = :hash
     *      WHERE vault_id = :id AND current_root_revision = :expected
     *   2. INSERT INTO vault_root_manifests (...)
     *
     * Returns null on success. On conflict returns the §A1-root payload
     * — caller forwards it verbatim into the 409 `vault_root_conflict`
     * response so the client can run the §D4 merge without an extra
     * round-trip.
     *
     * Both writes happen inside a single SQLite IMMEDIATE transaction so
     * a failure between them rolls back cleanly. Callers that need
     * shard + root atomicity wrap a separate outer BEGIN IMMEDIATE
     * (see ``VaultFolderShardsRepository::tryAtomicShardWithRootCAS``);
     * the nested call uses a savepoint-less path because SQLite's
     * BEGIN IMMEDIATE is already taken by the outer scope. F-S07.
     *
     * @return array{
     *     current_root_revision: int,
     *     current_root_hash: string,
     *     current_root_ciphertext: string,
     *     current_root_size: int
     * }|null
     */
    public function tryCAS(
        string $vaultId,
        int $expectedCurrentRootRevision,
        int $newRootRevision,
        string $rootHash,
        string $rootCiphertext,
        int $rootSize,
        string $authorDeviceId,
        int $now,
        bool $beginTransaction = true,
    ): ?array {
        if ($beginTransaction) {
            $this->db->execute('BEGIN IMMEDIATE');
        }
        try {
            $this->db->execute(
                'UPDATE vaults
                 SET current_root_revision = :new,
                     current_root_hash     = :hash,
                     updated_at            = :now
                 WHERE vault_id              = :id
                   AND current_root_revision = :expected',
                [
                    ':new'      => $newRootRevision,
                    ':hash'     => $rootHash,
                    ':now'      => $now,
                    ':id'       => $vaultId,
                    ':expected' => $expectedCurrentRootRevision,
                ]
            );
            if ($this->db->changes() !== 1) {
                $conflict = $this->fetchConflictPayload($vaultId);
                if ($beginTransaction) {
                    $this->db->execute('ROLLBACK');
                }
                return $conflict;
            }

            $this->create(
                $vaultId,
                $newRootRevision,
                $expectedCurrentRootRevision,
                $rootHash,
                $rootCiphertext,
                $rootSize,
                $authorDeviceId,
                $now
            );

            if ($beginTransaction) {
                $this->db->execute('COMMIT');
            }
            return null;
        } catch (\Throwable $e) {
            if ($beginTransaction) {
                $this->db->execute('ROLLBACK');
            }
            throw $e;
        }
    }

    /**
     * Read the current root into the §A1-root conflict shape. Called
     * when ``tryCAS`` sees a stale ``expected_current_root_revision`` so
     * the caller can forward this payload into the 409 error body and
     * the client can re-merge in one round-trip.
     */
    private function fetchConflictPayload(string $vaultId): array
    {
        $row = $this->db->querySingle(
            'SELECT vrm.root_revision           AS current_root_revision,
                    vrm.root_hash                AS current_root_hash,
                    vrm.root_ciphertext          AS current_root_ciphertext,
                    vrm.root_size                AS current_root_size
             FROM vault_root_manifests vrm
             INNER JOIN vaults v
                 ON v.vault_id = vrm.vault_id
                AND v.current_root_revision = vrm.root_revision
             WHERE vrm.vault_id = :id',
            [':id' => $vaultId]
        );
        if ($row === null) {
            return [
                'current_root_revision'   => 0,
                'current_root_hash'       => '',
                'current_root_ciphertext' => '',
                'current_root_size'       => 0,
            ];
        }
        return [
            'current_root_revision'   => (int)$row['current_root_revision'],
            'current_root_hash'       => (string)$row['current_root_hash'],
            'current_root_ciphertext' => (string)$row['current_root_ciphertext'],
            'current_root_size'       => (int)$row['current_root_size'],
        ];
    }
}
