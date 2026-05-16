<?php

/**
 * Owns the per-folder shard manifest history in `vault_folder_shards`
 * and the per-folder head pointer in `vault_folder_shard_heads`.
 * Mirrors ``VaultRootManifestsRepository`` for the root + `vaults` head
 * pair, but keyed by (vault_id, remote_folder_id) so each folder's CAS
 * chain advances independently.
 *
 * The hot path is ``tryCAS()`` for shard-only publishes and
 * ``tryAtomicShardWithRootCAS()`` for the spec's primary publish path
 * (§6.8 of `vault-v1.md`'s ``PUT /folders/{id}/shard-with-root``). The
 * atomic path uses a SELECT-then-UPDATE pattern under a single
 * BEGIN IMMEDIATE so a reader cannot observe a half-published pair.
 */
class VaultFolderShardsRepository
{
    public function __construct(private Database $db) {}

    /**
     * Insert any shard revision, including the genesis (parent = 0).
     * Caller keeps ``vault_folder_shard_heads`` in sync; the
     * head-row upsert pattern is encapsulated in ``tryCAS``.
     */
    public function create(
        string $vaultId,
        string $remoteFolderId,
        int $shardRevision,
        int $parentShardRevision,
        string $shardHash,
        string $shardCiphertext,
        int $shardSize,
        string $authorDeviceId,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO vault_folder_shards (
                vault_id,
                remote_folder_id,
                shard_revision,
                parent_shard_revision,
                shard_hash,
                shard_ciphertext,
                shard_size,
                author_device_id,
                created_at
             ) VALUES (
                :vault_id,
                :folder_id,
                :shard_revision,
                :parent_shard_revision,
                :hash,
                :ciphertext,
                :size,
                :author,
                :now
             )',
            [
                ':vault_id'              => $vaultId,
                ':folder_id'             => $remoteFolderId,
                ':shard_revision'        => $shardRevision,
                ':parent_shard_revision' => $parentShardRevision,
                ':hash'                  => $shardHash,
                ':ciphertext'            => new Blob($shardCiphertext),
                ':size'                  => $shardSize,
                ':author'                => $authorDeviceId,
                ':now'                   => $now,
            ]
        );
    }

    /**
     * The current head: the shard row whose `shard_revision` matches
     * `vault_folder_shard_heads.current_shard_revision` for the given
     * (vault, folder). Returns null when the folder has no shard yet
     * (a "remote_folders" pointer can exist on the root without a shard
     * envelope having been published yet, though the §6.8 flow keeps
     * them in sync).
     */
    public function getCurrent(string $vaultId, string $remoteFolderId): ?array
    {
        return $this->db->querySingle(
            'SELECT vfs.vault_id,
                    vfs.remote_folder_id,
                    vfs.shard_revision,
                    vfs.parent_shard_revision,
                    vfs.shard_hash,
                    vfs.shard_ciphertext,
                    vfs.shard_size,
                    vfs.author_device_id,
                    vfs.created_at
             FROM vault_folder_shards vfs
             INNER JOIN vault_folder_shard_heads h
                 ON h.vault_id          = vfs.vault_id
                AND h.remote_folder_id  = vfs.remote_folder_id
                AND h.current_shard_revision = vfs.shard_revision
             WHERE vfs.vault_id = :id
               AND vfs.remote_folder_id = :folder_id',
            [':id' => $vaultId, ':folder_id' => $remoteFolderId]
        );
    }

    /**
     * Lookup a specific historical shard revision (used by GC walks).
     */
    public function getByRevision(string $vaultId, string $remoteFolderId, int $shardRevision): ?array
    {
        return $this->db->querySingle(
            'SELECT vault_id,
                    remote_folder_id,
                    shard_revision,
                    parent_shard_revision,
                    shard_hash,
                    shard_ciphertext,
                    shard_size,
                    author_device_id,
                    created_at
             FROM vault_folder_shards
             WHERE vault_id = :id
               AND remote_folder_id = :folder_id
               AND shard_revision = :rev',
            [':id' => $vaultId, ':folder_id' => $remoteFolderId, ':rev' => $shardRevision]
        );
    }

    /**
     * CAS publish for a shard-only operation. Atomically:
     *
     *   1. INSERT OR IGNORE into vault_folder_shard_heads (genesis bootstrap)
     *   2. UPDATE vault_folder_shard_heads SET current_shard_revision = :new
     *      WHERE vault_id = :id AND remote_folder_id = :folder
     *        AND current_shard_revision = :expected
     *   3. INSERT INTO vault_folder_shards (...)
     *
     * Returns null on success. On conflict returns the §A1-shard
     * payload — caller forwards it verbatim into the 409
     * `vault_shard_conflict` response. ``beginTransaction = false`` lets
     * the atomic shard-with-root path nest this inside an outer
     * transaction.
     *
     * @return array{
     *     remote_folder_id: string,
     *     current_shard_revision: int,
     *     current_shard_hash: string,
     *     current_shard_ciphertext: string,
     *     current_shard_size: int
     * }|null
     */
    public function tryCAS(
        string $vaultId,
        string $remoteFolderId,
        int $expectedCurrentShardRevision,
        int $newShardRevision,
        string $shardHash,
        string $shardCiphertext,
        int $shardSize,
        string $authorDeviceId,
        int $now,
        bool $beginTransaction = true,
    ): ?array {
        if ($beginTransaction) {
            $this->db->execute('BEGIN IMMEDIATE');
        }
        try {
            // 1. Bootstrap a head row for first-publish folders. Existing
            //    folders are no-op'd by the OR IGNORE because of the PK.
            $this->db->execute(
                'INSERT OR IGNORE INTO vault_folder_shard_heads (
                    vault_id, remote_folder_id, current_shard_revision,
                    current_shard_hash, updated_at
                 ) VALUES (:id, :folder_id, 0, \'\', :now)',
                [':id' => $vaultId, ':folder_id' => $remoteFolderId, ':now' => $now],
            );

            // 2. Conditional UPDATE — the CAS primitive.
            $this->db->execute(
                'UPDATE vault_folder_shard_heads
                 SET current_shard_revision = :new,
                     current_shard_hash     = :hash,
                     updated_at             = :now
                 WHERE vault_id               = :id
                   AND remote_folder_id       = :folder_id
                   AND current_shard_revision = :expected',
                [
                    ':new'       => $newShardRevision,
                    ':hash'      => $shardHash,
                    ':now'       => $now,
                    ':id'        => $vaultId,
                    ':folder_id' => $remoteFolderId,
                    ':expected'  => $expectedCurrentShardRevision,
                ]
            );
            if ($this->db->changes() !== 1) {
                $conflict = $this->fetchConflictPayload($vaultId, $remoteFolderId);
                if ($beginTransaction) {
                    $this->db->execute('ROLLBACK');
                }
                return $conflict;
            }

            // 3. Insert the immutable history row.
            $this->create(
                $vaultId,
                $remoteFolderId,
                $newShardRevision,
                $expectedCurrentShardRevision,
                $shardHash,
                $shardCiphertext,
                $shardSize,
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
     * Atomic shard + root CAS (§6.8). Pre-checks both head pointers
     * under one BEGIN IMMEDIATE, then commits both writes together — or
     * rolls back and returns the appropriate conflict shape.
     *
     * Pre-checking (SELECT) before any UPDATE is the only safe pattern
     * for an "all or nothing" pair: a conditional UPDATE that fails
     * leaves us with no easy way to know whether the OTHER side would
     * have succeeded, and the atomic-publish 409 needs to tell the
     * client about both stale revisions when both drifted. WAL-mode
     * SQLite + BEGIN IMMEDIATE locks out other writers from the moment
     * the SELECT runs, so SELECT-then-UPDATE inside a single IMMEDIATE
     * is race-free.
     *
     * Returns:
     *   - `null` on success.
     *   - `['kind' => 'shard', 'shard' => ...]`         on shard-only conflict.
     *   - `['kind' => 'root',  'root' => ...]`          on root-only conflict.
     *   - `['kind' => 'shard_root', 'shard' => ..., 'root' => ...]` when both stale.
     */
    public function tryAtomicShardWithRootCAS(
        string $vaultId,
        string $remoteFolderId,
        int    $expectedCurrentShardRevision,
        int    $newShardRevision,
        string $shardHash,
        string $shardCiphertext,
        int    $shardSize,
        int    $expectedCurrentRootRevision,
        int    $newRootRevision,
        string $rootHash,
        string $rootCiphertext,
        int    $rootSize,
        string $authorDeviceId,
        int    $now,
        VaultRootManifestsRepository $rootRepo,
    ): ?array {
        $this->db->execute('BEGIN IMMEDIATE');
        try {
            // Bootstrap a shard head row for first-publish folders. For
            // an existing row the OR IGNORE is a no-op.
            $this->db->execute(
                'INSERT OR IGNORE INTO vault_folder_shard_heads (
                    vault_id, remote_folder_id, current_shard_revision,
                    current_shard_hash, updated_at
                 ) VALUES (:id, :folder_id, 0, \'\', :now)',
                [':id' => $vaultId, ':folder_id' => $remoteFolderId, ':now' => $now],
            );

            // Peek both heads under the IMMEDIATE lock.
            $shardHeadRow = $this->db->querySingle(
                'SELECT current_shard_revision
                 FROM vault_folder_shard_heads
                 WHERE vault_id = :id AND remote_folder_id = :folder_id',
                [':id' => $vaultId, ':folder_id' => $remoteFolderId],
            );
            $rootHeadRow = $this->db->querySingle(
                'SELECT current_root_revision
                 FROM vaults
                 WHERE vault_id = :id',
                [':id' => $vaultId],
            );

            $shardCurrent = $shardHeadRow !== null
                ? (int)$shardHeadRow['current_shard_revision']
                : 0;
            $rootCurrent = $rootHeadRow !== null
                ? (int)$rootHeadRow['current_root_revision']
                : 0;

            $shardStale = $shardCurrent !== $expectedCurrentShardRevision;
            $rootStale  = $rootCurrent  !== $expectedCurrentRootRevision;

            if ($shardStale || $rootStale) {
                $shardConflict = $shardStale
                    ? $this->fetchConflictPayload($vaultId, $remoteFolderId)
                    : null;
                $rootConflict  = $rootStale
                    ? $this->fetchRootConflictPayload($vaultId)
                    : null;
                $this->db->execute('ROLLBACK');

                if ($shardStale && $rootStale) {
                    return [
                        'kind'  => 'shard_root',
                        'shard' => $shardConflict,
                        'root'  => $rootConflict,
                    ];
                }
                if ($shardStale) {
                    return ['kind' => 'shard', 'shard' => $shardConflict];
                }
                return ['kind' => 'root', 'root' => $rootConflict];
            }

            // Both heads fresh — commit both writes.
            $this->db->execute(
                'UPDATE vault_folder_shard_heads
                 SET current_shard_revision = :new,
                     current_shard_hash     = :hash,
                     updated_at             = :now
                 WHERE vault_id               = :id
                   AND remote_folder_id       = :folder_id
                   AND current_shard_revision = :expected',
                [
                    ':new'       => $newShardRevision,
                    ':hash'      => $shardHash,
                    ':now'       => $now,
                    ':id'        => $vaultId,
                    ':folder_id' => $remoteFolderId,
                    ':expected'  => $expectedCurrentShardRevision,
                ]
            );
            if ($this->db->changes() !== 1) {
                // The peek said fresh but the UPDATE didn't match — that
                // can only happen if another writer slipped between
                // SELECT and UPDATE, which BEGIN IMMEDIATE forbids.
                // Surface as a generic conflict and let the client
                // retry; this defends against driver-level oddities.
                $conflict = $this->fetchConflictPayload($vaultId, $remoteFolderId);
                $this->db->execute('ROLLBACK');
                return ['kind' => 'shard', 'shard' => $conflict];
            }

            $this->create(
                $vaultId,
                $remoteFolderId,
                $newShardRevision,
                $expectedCurrentShardRevision,
                $shardHash,
                $shardCiphertext,
                $shardSize,
                $authorDeviceId,
                $now,
            );

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
                $conflict = $this->fetchRootConflictPayload($vaultId);
                $this->db->execute('ROLLBACK');
                return ['kind' => 'root', 'root' => $conflict];
            }

            $rootRepo->create(
                $vaultId,
                $newRootRevision,
                $expectedCurrentRootRevision,
                $rootHash,
                $rootCiphertext,
                $rootSize,
                $authorDeviceId,
                $now,
            );

            $this->db->execute('COMMIT');
            return null;
        } catch (\Throwable $e) {
            try { $this->db->execute('ROLLBACK'); } catch (\Throwable $ignored) {}
            throw $e;
        }
    }

    /**
     * Read the current shard into the §A1-shard conflict shape. Used
     * after a CAS UPDATE didn't match.
     */
    private function fetchConflictPayload(string $vaultId, string $remoteFolderId): array
    {
        $row = $this->db->querySingle(
            'SELECT vfs.remote_folder_id    AS remote_folder_id,
                    vfs.shard_revision      AS current_shard_revision,
                    vfs.shard_hash          AS current_shard_hash,
                    vfs.shard_ciphertext    AS current_shard_ciphertext,
                    vfs.shard_size          AS current_shard_size
             FROM vault_folder_shards vfs
             INNER JOIN vault_folder_shard_heads h
                 ON h.vault_id              = vfs.vault_id
                AND h.remote_folder_id      = vfs.remote_folder_id
                AND h.current_shard_revision = vfs.shard_revision
             WHERE vfs.vault_id = :id
               AND vfs.remote_folder_id = :folder_id',
            [':id' => $vaultId, ':folder_id' => $remoteFolderId]
        );
        if ($row === null) {
            return [
                'remote_folder_id'         => $remoteFolderId,
                'current_shard_revision'   => 0,
                'current_shard_hash'       => '',
                'current_shard_ciphertext' => '',
                'current_shard_size'       => 0,
            ];
        }
        return [
            'remote_folder_id'         => (string)$row['remote_folder_id'],
            'current_shard_revision'   => (int)$row['current_shard_revision'],
            'current_shard_hash'       => (string)$row['current_shard_hash'],
            'current_shard_ciphertext' => (string)$row['current_shard_ciphertext'],
            'current_shard_size'       => (int)$row['current_shard_size'],
        ];
    }

    /**
     * §A1-root conflict shape — same SELECT as
     * ``VaultRootManifestsRepository::fetchConflictPayload`` but local
     * to this repo so the atomic path can inline it without crossing
     * the encapsulation boundary.
     */
    private function fetchRootConflictPayload(string $vaultId): array
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
