<?php

/**
 * Owns all SQL touching the `pairings` and `pairing_requests` tables.
 * Pairing authorization (`findPairing`) is invoked from both the ping
 * handler and the fasttrack handler — keeping it here avoids the
 * duplicated OR-clause query that used to live in both controllers.
 */
class PairingRepository
{
    public function __construct(private Database $db) {}

    /**
     * Order-independent pairing lookup. Callers pass the two device IDs
     * in any order; the repository checks both (a,b) and (b,a).
     */
    public function findPairing(string $a, string $b): ?array
    {
        return $this->db->querySingle(
            'SELECT id FROM pairings
             WHERE (device_a_id = :a AND device_b_id = :b)
                OR (device_a_id = :b2 AND device_b_id = :a2)',
            [':a' => $a, ':b' => $b, ':a2' => $a, ':b2' => $b]
        );
    }

    /**
     * Caller is responsible for pre-sorting IDs so (device_a_id,
     * device_b_id) stays normalized and the UNIQUE constraint holds.
     */
    public function createPairing(string $a, string $b, int $now): void
    {
        $this->db->execute(
            'INSERT INTO pairings (device_a_id, device_b_id, created_at) VALUES (:a, :b, :now)',
            [':a' => $a, ':b' => $b, ':now' => $now]
        );
    }

    public function listPairingsForDevice(string $deviceId): array
    {
        return $this->db->queryAll(
            'SELECT * FROM pairings WHERE device_a_id = :id OR device_b_id = :id',
            [':id' => $deviceId]
        );
    }

    /**
     * Both IDs must be pre-sorted (caller's responsibility), matching the
     * UNIQUE(device_a_id, device_b_id) index order used by createPairing.
     */
    public function incrementPairingStats(string $a, string $b, int $bytes): void
    {
        $this->db->execute(
            'UPDATE pairings
             SET bytes_transferred = bytes_transferred + :bytes,
                 transfer_count = transfer_count + 1
             WHERE device_a_id = :a AND device_b_id = :b',
            [':bytes' => $bytes, ':a' => $a, ':b' => $b]
        );
    }

    public function insertPairingRequest(
        string $desktopId,
        string $phoneId,
        string $phonePubkey,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO pairing_requests (desktop_id, phone_id, phone_pubkey, created_at)
             VALUES (:desktop, :phone, :pubkey, :now)',
            [
                ':desktop' => $desktopId,
                ':phone' => $phoneId,
                ':pubkey' => $phonePubkey,
                ':now' => $now,
            ]
        );
    }

    public function deleteUnclaimedRequests(string $phoneId, string $desktopId): void
    {
        $this->db->execute(
            'DELETE FROM pairing_requests WHERE phone_id = :phone AND desktop_id = :desktop AND claimed = 0',
            [':phone' => $phoneId, ':desktop' => $desktopId]
        );
    }

    public function deleteRequestsBetweenDevices(string $a, string $b): void
    {
        $this->db->execute(
            'DELETE FROM pairing_requests
             WHERE (desktop_id = :a AND phone_id = :b)
                OR (desktop_id = :b2 AND phone_id = :a2)',
            [':a' => $a, ':b' => $b, ':a2' => $a, ':b2' => $b]
        );
    }

    public function listUnclaimedRequestsForDesktop(string $desktopId): array
    {
        return $this->db->queryAll(
            'SELECT id, phone_id, phone_pubkey FROM pairing_requests
             WHERE desktop_id = :desktop AND claimed = 0
             ORDER BY created_at ASC',
            [':desktop' => $desktopId]
        );
    }

    public function markRequestClaimed(int $requestId): void
    {
        $this->db->execute(
            'UPDATE pairing_requests SET claimed = 1 WHERE id = :id',
            [':id' => $requestId]
        );
    }

    public function deleteExpiredRequests(int $cutoff): void
    {
        $this->db->execute(
            'DELETE FROM pairing_requests WHERE created_at < :cutoff',
            [':cutoff' => $cutoff]
        );
    }

    public function findAll(): array
    {
        return $this->db->queryAll('SELECT * FROM pairings ORDER BY created_at DESC');
    }

    /**
     * Duplicate-pairing check used by the confirm step. Expects
     * pre-sorted IDs (same contract as createPairing).
     */
    public function findSortedPairing(string $a, string $b): ?array
    {
        return $this->db->querySingle(
            'SELECT id FROM pairings WHERE device_a_id = :a AND device_b_id = :b',
            [':a' => $a, ':b' => $b]
        );
    }
}
