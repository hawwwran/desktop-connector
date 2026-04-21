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
        int $now,
        string $mode = 'classic',
    ): void {
        $this->db->execute(
            'INSERT INTO transfers (id, sender_id, recipient_id, encrypted_meta, chunk_count, created_at, mode)
             VALUES (:id, :sender, :recipient, :meta, :chunks, :now, :mode)',
            [
                ':id' => $id,
                ':sender' => $senderId,
                ':recipient' => $recipientId,
                ':meta' => $encryptedMeta,
                ':chunks' => $chunkCount,
                ':now' => $now,
                ':mode' => $mode,
            ]
        );
    }

    /** Full row. Callers pick the fields they need. */
    public function findById(string $transferId): ?array
    {
        return $this->db->querySingle(
            'SELECT id, sender_id, recipient_id, encrypted_meta, chunk_count,
                    chunks_received, complete, created_at, downloaded,
                    delivered_at, chunks_downloaded,
                    mode, aborted, abort_reason, aborted_at,
                    stream_ready_at, chunks_uploaded
             FROM transfers WHERE id = :id',
            [':id' => $transferId]
        );
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

    /**
     * Classic mode returns transfers with complete=1 (upload finished);
     * streaming returns transfers as soon as stream_ready_at is stamped
     * (first chunk stored) so the recipient can start pulling
     * immediately. Aborted rows are filtered out — recipients shouldn't
     * see an aborted transfer in pending and try to download it.
     */
    public function listPendingForRecipient(string $recipientId): array
    {
        return $this->db->queryAll(
            "SELECT id as transfer_id, sender_id, encrypted_meta, chunk_count, created_at, mode
             FROM transfers
             WHERE recipient_id = :rid AND aborted = 0 AND downloaded = 0
               AND (
                 (mode = 'classic'   AND complete = 1)
                 OR (mode = 'streaming' AND stream_ready_at IS NOT NULL AND stream_ready_at > 0)
               )
             ORDER BY created_at ASC",
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
    /**
     * $onlyComplete=true historically narrowed to uploads that had at
     * least finished server-side (the /notify inline payload cared only
     * about delivery progression). Streaming transfers expose progress
     * before complete=1, so the "interesting" filter now also allows
     * aborted rows and streaming rows — in both cases the sender wants
     * to paint a new state on its local row.
     */
    public function loadSentForDevice(string $deviceId, int $limit = 50, bool $onlyComplete = false): array
    {
        $where = 'sender_id = :sid';
        if ($onlyComplete) {
            $where .= " AND (complete = 1 OR aborted = 1 OR mode = 'streaming')";
        }
        return $this->db->queryAll(
            "SELECT id AS transfer_id, recipient_id, complete, downloaded, chunk_count, chunks_downloaded, created_at,
                    mode, aborted, abort_reason, chunks_uploaded
             FROM transfers WHERE $where ORDER BY created_at DESC LIMIT " . (int)$limit,
            [':sid' => $deviceId]
        );
    }

    /**
     * Baseline progress sum used by /notify long-poll to detect
     * recipient-side chunk advancement. Previously filtered to
     * `complete = 1` — that hid streaming transfers whose recipient
     * already started ACKing chunks while the upload was in flight.
     * Aborted rows are filtered so a cleanup race doesn't mask
     * legitimate progress elsewhere.
     */
    public function sumSentChunksDownloaded(string $deviceId): int
    {
        $row = $this->db->querySingle(
            'SELECT COALESCE(SUM(chunks_downloaded), 0) as total FROM transfers
             WHERE sender_id = :sid AND downloaded = 0 AND aborted = 0',
            [':sid' => $deviceId]
        );
        return (int)($row['total'] ?? 0);
    }

    /**
     * Matches the filter used by listPendingForRecipient so /notify
     * long-poll fires on streaming transfers as soon as chunk 0 lands,
     * not after the full upload completes.
     */
    public function countPendingForRecipient(string $recipientId): int
    {
        $row = $this->db->querySingle(
            "SELECT COUNT(*) as count FROM transfers
             WHERE recipient_id = :rid AND aborted = 0 AND downloaded = 0
               AND (
                 (mode = 'classic'   AND complete = 1)
                 OR (mode = 'streaming' AND stream_ready_at IS NOT NULL AND stream_ready_at > 0)
               )",
            [':rid' => $recipientId]
        );
        return (int)($row['count'] ?? 0);
    }

    /**
     * Total chunk_count across every transfer still owed to this
     * recipient (not yet fully downloaded). Used at init to project
     * reserved storage (chunks * CHUNK_SIZE) and enforce the per-
     * recipient quota before we accept a new transfer.
     */
    public function sumPendingChunkCountForRecipient(string $recipientId): int
    {
        $row = $this->db->querySingle(
            'SELECT COALESCE(SUM(chunk_count), 0) as total FROM transfers
             WHERE recipient_id = :rid AND downloaded = 0',
            [':rid' => $recipientId]
        );
        return (int)($row['total'] ?? 0);
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

    // --- Streaming-relay support (migration 002) ----------------------------

    /**
     * Stamp the first-chunk-stored moment for a streaming transfer.
     * Idempotent via the WHERE clause — only the first caller actually
     * writes, so the `stream_ready` FCM wake fires exactly once.
     */
    public function markStreamReady(string $transferId, int $now): bool
    {
        $this->db->execute(
            'UPDATE transfers
             SET stream_ready_at = :now
             WHERE id = :id AND (stream_ready_at IS NULL OR stream_ready_at = 0)',
            [':now' => $now, ':id' => $transferId]
        );
        return $this->db->changes() > 0;
    }

    /**
     * Mark the transfer aborted. The partial UPDATE is idempotent via the
     * aborted=0 guard: a second DELETE request from either party sees the
     * row already flipped and can short-circuit to a 410 without a race
     * against concurrent cleanup. The downloaded=0 guard closes the race
     * with a concurrent final per-chunk ACK — once the recipient has
     * finalized the transfer, the bytes are theirs and abort is a no-op.
     */
    public function markAborted(string $transferId, string $reason, int $now): bool
    {
        $this->db->execute(
            'UPDATE transfers
             SET aborted = 1, abort_reason = :reason, aborted_at = :now
             WHERE id = :id AND aborted = 0 AND downloaded = 0',
            [':reason' => $reason, ':now' => $now, ':id' => $transferId]
        );
        return $this->db->changes() > 0;
    }

    /** Streaming-only. Classic mode keeps chunks_uploaded at 0. */
    public function incrementChunksUploaded(string $transferId): void
    {
        $this->db->execute(
            'UPDATE transfers SET chunks_uploaded = chunks_uploaded + 1 WHERE id = :id',
            [':id' => $transferId]
        );
    }
}
