<?php

/**
 * Owns all SQL touching the `fasttrack_messages` table. FCM wake and
 * payload interpretation stay with the calling controller — this
 * repository only handles message persistence.
 */
class FasttrackRepository
{
    public function __construct(private Database $db) {}

    public function deleteExpiredForRecipient(string $recipientId, int $cutoff): void
    {
        $this->db->execute(
            'DELETE FROM fasttrack_messages WHERE recipient_id = :rid AND created_at < :cutoff',
            [':rid' => $recipientId, ':cutoff' => $cutoff]
        );
    }

    public function countPendingForRecipient(string $recipientId): int
    {
        $row = $this->db->querySingle(
            'SELECT COUNT(*) as cnt FROM fasttrack_messages WHERE recipient_id = :rid',
            [':rid' => $recipientId]
        );
        return (int)($row['cnt'] ?? 0);
    }

    /** Returns the newly inserted message id. */
    public function insertMessage(
        string $senderId,
        string $recipientId,
        string $encryptedData,
        int $now
    ): int {
        $this->db->execute(
            'INSERT INTO fasttrack_messages (sender_id, recipient_id, encrypted_data, created_at)
             VALUES (:sid, :rid, :data, :now)',
            [':sid' => $senderId, ':rid' => $recipientId, ':data' => $encryptedData, ':now' => $now]
        );
        return $this->db->lastInsertId();
    }

    public function listPendingForRecipient(string $recipientId): array
    {
        return $this->db->queryAll(
            'SELECT id, sender_id, encrypted_data, created_at
             FROM fasttrack_messages
             WHERE recipient_id = :rid
             ORDER BY created_at ASC',
            [':rid' => $recipientId]
        );
    }

    public function findById(int $messageId): ?array
    {
        return $this->db->querySingle(
            'SELECT recipient_id FROM fasttrack_messages WHERE id = :id',
            [':id' => $messageId]
        );
    }

    public function deleteById(int $messageId): void
    {
        $this->db->execute(
            'DELETE FROM fasttrack_messages WHERE id = :id',
            [':id' => $messageId]
        );
    }
}
