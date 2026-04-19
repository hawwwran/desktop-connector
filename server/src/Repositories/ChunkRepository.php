<?php

/**
 * Owns all SQL touching the `chunks` table. File I/O against
 * `server/storage/` stays with the calling service — this repository
 * only handles chunk metadata in the database.
 *
 * The chunks⋈transfers JOIN used for the recipient storage-limit
 * check lives here rather than in TransferRepository because the
 * aggregation is over chunk sizes; the transfer join is just a
 * filter.
 */
class ChunkRepository
{
    public function __construct(private Database $db) {}

    public function findChunk(string $transferId, int $chunkIndex): ?array
    {
        return $this->db->querySingle(
            'SELECT blob_path, blob_size FROM chunks WHERE transfer_id = :tid AND chunk_index = :idx',
            [':tid' => $transferId, ':idx' => $chunkIndex]
        );
    }

    public function insertChunk(
        string $transferId,
        int $chunkIndex,
        string $blobPath,
        int $blobSize,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO chunks (transfer_id, chunk_index, blob_path, blob_size, created_at)
             VALUES (:tid, :idx, :path, :size, :now)',
            [
                ':tid' => $transferId,
                ':idx' => $chunkIndex,
                ':path' => $blobPath,
                ':size' => $blobSize,
                ':now' => $now,
            ]
        );
    }

    public function chunkExists(string $transferId, int $chunkIndex): bool
    {
        $row = $this->db->querySingle(
            'SELECT chunk_index FROM chunks WHERE transfer_id = :tid AND chunk_index = :idx',
            [':tid' => $transferId, ':idx' => $chunkIndex]
        );
        return $row !== null;
    }

    /** Returns rows with `blob_path` set — used by the cleanup path that deletes files. */
    public function listChunksForTransfer(string $transferId): array
    {
        return $this->db->queryAll(
            'SELECT blob_path FROM chunks WHERE transfer_id = :tid',
            [':tid' => $transferId]
        );
    }

    public function sumChunkBytesForTransfer(string $transferId): int
    {
        $row = $this->db->querySingle(
            'SELECT COALESCE(SUM(blob_size), 0) as total FROM chunks WHERE transfer_id = :tid',
            [':tid' => $transferId]
        );
        return (int)($row['total'] ?? 0);
    }

    /**
     * Total bytes stored for a recipient across all transfers that
     * haven't been acknowledged yet. Used to enforce the per-recipient
     * storage limit on init. JOIN is against transfers because the
     * chunks table doesn't carry recipient_id directly.
     */
    public function sumPendingBytesForRecipient(string $recipientId): int
    {
        $row = $this->db->querySingle(
            'SELECT COALESCE(SUM(c.blob_size), 0) as total_bytes
             FROM chunks c
             JOIN transfers t ON c.transfer_id = t.id
             WHERE t.recipient_id = :rid AND t.downloaded = 0',
            [':rid' => $recipientId]
        );
        return (int)($row['total_bytes'] ?? 0);
    }

    public function deleteChunksForTransfer(string $transferId): void
    {
        $this->db->execute(
            'DELETE FROM chunks WHERE transfer_id = :tid',
            [':tid' => $transferId]
        );
    }

    /** Dashboard "total storage used" figure across every pending chunk. */
    public function sumAllBytes(): int
    {
        $row = $this->db->querySingle(
            'SELECT COALESCE(SUM(blob_size), 0) as total FROM chunks'
        );
        return (int)($row['total'] ?? 0);
    }
}
