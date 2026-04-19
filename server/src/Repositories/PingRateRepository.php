<?php

/**
 * Owns the atomic rate-limit / debounce slot for ping requests. The
 * UPSERT shape here is load-bearing: its `WHERE ping_rate.cooldown_until
 * <= :now` clause is what keeps concurrent or back-to-back pings for the
 * same (sender, recipient) pair from bypassing the cooldown under WAL
 * serialization. Do not split into SELECT-then-UPDATE — that would open a
 * race the current design closes.
 */
class PingRateRepository
{
    public function __construct(private Database $db) {}

    /**
     * Atomic UPSERT: returns true iff this caller successfully claimed
     * the cooldown slot. Returns false when another caller holds a live
     * cooldown (the conditional UPDATE's WHERE filters it out and
     * changes() is 0).
     */
    public function tryClaimCooldown(
        string $senderId,
        string $recipientId,
        int $cooldownUntil,
        int $now
    ): bool {
        $this->db->execute(
            'INSERT INTO ping_rate (sender_id, recipient_id, cooldown_until)
             VALUES (:s, :r, :until)
             ON CONFLICT(sender_id, recipient_id) DO UPDATE
             SET cooldown_until = excluded.cooldown_until
             WHERE ping_rate.cooldown_until <= :now',
            [':s' => $senderId, ':r' => $recipientId, ':until' => $cooldownUntil, ':now' => $now]
        );
        return $this->db->changes() === 1;
    }

    /**
     * Read the currently-active cooldown_until so the caller can compute
     * Retry-After. Only called on the 429 branch after tryClaimCooldown
     * returns false.
     */
    public function findCooldown(string $senderId, string $recipientId): ?int
    {
        $row = $this->db->querySingle(
            'SELECT cooldown_until FROM ping_rate WHERE sender_id = :s AND recipient_id = :r',
            [':s' => $senderId, ':r' => $recipientId]
        );
        return $row ? (int)$row['cooldown_until'] : null;
    }
}
