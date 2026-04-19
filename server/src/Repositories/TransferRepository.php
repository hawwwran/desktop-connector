<?php

/**
 * Owns all SQL touching the `transfers` table. Services express the
 * intent ("start transfer", "mark complete", "load sender's recent");
 * this repository holds the queries and consolidates the three partial
 * SELECT shapes (sender/recipient/complete subsets) that used to live
 * in TransferService into one full-row findById.
 *
 * The `loadSentForDevice` query's `LIMIT` value is embedded into the
 * SQL string (after an `(int)` cast) because SQLite prepared statements
 * don't accept bound LIMIT values; this matches the pre-refactor shape.
 */
class TransferRepository
{
    public function __construct(private Database $db) {}

    public function existsById(string $transferId): bool
    {
        $row = $this->db->querySingle(
            'SELECT id FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        return $row !== null;
    }

    public function insertTransfer(
        string $id,
        string $senderId,
        string $recipientId,
        string $encryptedMeta,
        int $chunkCount,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO transfers (id, sender_id, recipient_id, encrypted_meta, chunk_count, created_at)
             VALUES (:id, :sender, :recipient, :meta, :chunks, :now)',
            [
                ':id' => $id,
                ':sender' => $senderId,
                ':recipient' => $recipientId,
                ':meta' => $encryptedMeta,
                ':chunks' => $chunkCount,
                ':now' => $now,
            ]
        );
    }

    /** Full row. Callers pick the fields they need. */
    public function findById(string $transferId): ?array
    {
        $row = $this->db->querySingle(
            'SELECT id, sender_id, recipient_id, encrypted_meta, chunk_count,
                    chunks_received, complete, created_at, downloaded,
                    delivered_at, chunks_downloaded
             FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
        if ($row !== null && self::invariantDebugEnabled()) {
            TransferInvariants::assertValid($row);
        }
        return $row;
    }

    private static function invariantDebugEnabled(): bool
    {
        return getenv('TRANSFER_INVARIANTS_DEBUG') === '1';
    }

    public function incrementChunksReceived(string $transferId): void
    {
        $this->db->execute(
            'UPDATE transfers SET chunks_received = chunks_received + 1 WHERE id = :id',
            [':id' => $transferId]
        );
    }

    public function markComplete(string $transferId): void
    {
        $this->db->execute(
            'UPDATE transfers SET complete = 1 WHERE id = :id',
            [':id' => $transferId]
        );
    }

    public function listPendingForRecipient(string $recipientId): array
    {
        return $this->db->queryAll(
            'SELECT id as transfer_id, sender_id, encrypted_meta, chunk_count, created_at
             FROM transfers
             WHERE recipient_id = :rid AND complete = 1 AND downloaded = 0
             ORDER BY created_at ASC',
            [':rid' => $recipientId]
        );
    }

    /**
     * Preserves the MAX() idiom: callers pass a computed target progress
     * and the UPDATE never regresses the counter (critical for concurrent
     * chunk downloads).
     */
    public function updateDownloadProgress(string $transferId, int $progress): void
    {
        $this->db->execute(
            'UPDATE transfers
             SET chunks_downloaded = MAX(chunks_downloaded, :progress)
             WHERE id = :id',
            [':progress' => $progress, ':id' => $transferId]
        );
    }

    /**
     * Final ACK state: downloaded=1, delivered_at set, chunks_downloaded
     * bumped to chunk_count. The invariant
     * `chunks_downloaded == chunk_count ⇔ downloaded == 1` depends on
     * this SQL — do not split.
     */
    public function markDelivered(string $transferId, int $now): void
    {
        $this->db->execute(
            'UPDATE transfers SET downloaded = 1, delivered_at = :now, chunks_downloaded = chunk_count
             WHERE id = :id',
            [':now' => $now, ':id' => $transferId]
        );
    }

    public function findExpired(int $cutoff): array
    {
        return $this->db->queryAll(
            'SELECT id FROM transfers WHERE created_at < :cutoff',
            [':cutoff' => $cutoff]
        );
    }

    public function findExpiredIncomplete(int $cutoff): array
    {
        return $this->db->queryAll(
            'SELECT id FROM transfers WHERE complete = 0 AND created_at < :cutoff',
            [':cutoff' => $cutoff]
        );
    }

    public function delete(string $transferId): void
    {
        $this->db->execute(
            'DELETE FROM transfers WHERE id = :tid',
            [':tid' => $transferId]
        );
    }

    /**
     * Shared by /sent-status ($onlyComplete=false) and /notify inline
     * payload ($onlyComplete=true). Single source of truth for both
     * paths so they can't drift.
     */
    public function loadSentForDevice(string $deviceId, int $limit = 50, bool $onlyComplete = false): array
    {
        $where = 'sender_id = :sid' . ($onlyComplete ? ' AND complete = 1' : '');
        return $this->db->queryAll(
            "SELECT id AS transfer_id, recipient_id, complete, downloaded, chunk_count, chunks_downloaded, created_at
             FROM transfers WHERE $where ORDER BY created_at DESC LIMIT " . (int)$limit,
            [':sid' => $deviceId]
        );
    }

    /**
     * Baseline progress sum used by /notify long-poll to detect
     * recipient-side chunk advancement.
     */
    public function sumSentChunksDownloaded(string $deviceId): int
    {
        $row = $this->db->querySingle(
            'SELECT COALESCE(SUM(chunks_downloaded), 0) as total FROM transfers
             WHERE sender_id = :sid AND complete = 1 AND downloaded = 0',
            [':sid' => $deviceId]
        );
        return (int)($row['total'] ?? 0);
    }

    public function countPendingForRecipient(string $recipientId): int
    {
        $row = $this->db->querySingle(
            'SELECT COUNT(*) as count FROM transfers
             WHERE recipient_id = :rid AND complete = 1 AND downloaded = 0',
            [':rid' => $recipientId]
        );
        return (int)($row['count'] ?? 0);
    }

    public function countDeliveredSinceForSender(string $senderId, int $since): int
    {
        $row = $this->db->querySingle(
            'SELECT COUNT(*) as count FROM transfers
             WHERE sender_id = :sid AND delivered_at >= :since',
            [':sid' => $senderId, ':since' => $since]
        );
        return (int)($row['count'] ?? 0);
    }

    /**
     * Stats helper — split into paired and unpaired variants to mirror
     * the existing branch in DeviceController::stats.
     */
    public function countPendingIncomingForPair(string $recipientId, string $senderId): array
    {
        $row = $this->db->querySingle(
            'SELECT COUNT(*) as count, COALESCE(SUM(chunk_count), 0) as chunks
             FROM transfers WHERE recipient_id = :id AND sender_id = :paired
             AND complete = 1 AND downloaded = 0',
            [':id' => $recipientId, ':paired' => $senderId]
        );
        return $row ?? ['count' => 0, 'chunks' => 0];
    }

    public function countPendingIncomingForDevice(string $deviceId): array
    {
        $row = $this->db->querySingle(
            'SELECT COUNT(*) as count, COALESCE(SUM(chunk_count), 0) as chunks
             FROM transfers WHERE recipient_id = :id AND complete = 1 AND downloaded = 0',
            [':id' => $deviceId]
        );
        return $row ?? ['count' => 0, 'chunks' => 0];
    }

    public function countPendingOutgoingForPair(string $senderId, string $recipientId): int
    {
        $row = $this->db->querySingle(
            'SELECT COUNT(*) as count FROM transfers
             WHERE sender_id = :id AND recipient_id = :paired AND downloaded = 0',
            [':id' => $senderId, ':paired' => $recipientId]
        );
        return (int)($row['count'] ?? 0);
    }

    public function countPendingOutgoingForDevice(string $deviceId): int
    {
        $row = $this->db->querySingle(
            'SELECT COUNT(*) as count FROM transfers
             WHERE sender_id = :id AND downloaded = 0',
            [':id' => $deviceId]
        );
        return (int)($row['count'] ?? 0);
    }

    /**
     * Dashboard list of pending (undownloaded) transfers. The per-row
     * byte aggregation is resolved separately by ChunkRepository in the
     * dashboard controller.
     */
    public function listPendingForDashboard(): array
    {
        return $this->db->queryAll(
            'SELECT * FROM transfers WHERE downloaded = 0 ORDER BY created_at DESC'
        );
    }

    /** Dashboard stats helper: count transfers by complete/downloaded flags. */
    public function countPendingByCompleteDownloaded(int $complete, int $downloaded): int
    {
        $row = $this->db->querySingle(
            'SELECT COUNT(*) as count FROM transfers WHERE complete = :c AND downloaded = :d',
            [':c' => $complete, ':d' => $downloaded]
        );
        return (int)($row['count'] ?? 0);
    }
}
