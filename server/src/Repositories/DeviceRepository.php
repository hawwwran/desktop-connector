<?php

/**
 * Owns all SQL touching the `devices` table. Services and controllers
 * express intent ("look up this device", "bump last_seen", "store FCM
 * token"); this repository holds the queries and the column-name
 * assumptions.
 */
class DeviceRepository
{
    public function __construct(private Database $db) {}

    public function findById(string $deviceId): ?array
    {
        return $this->db->querySingle(
            'SELECT device_id, public_key, auth_token, device_type, created_at, last_seen_at, fcm_token
             FROM devices WHERE device_id = :id',
            [':id' => $deviceId]
        );
    }

    /**
     * Used by AuthService to validate Bearer-token auth. Returns the row
     * on a credential match, null otherwise — callers rely on the null
     * check, not on specific fields.
     */
    public function findByCredentials(string $deviceId, string $authToken): ?array
    {
        return $this->db->querySingle(
            'SELECT device_id FROM devices WHERE device_id = :id AND auth_token = :token',
            [':id' => $deviceId, ':token' => $authToken]
        );
    }

    public function insertDevice(
        string $deviceId,
        string $publicKey,
        string $authToken,
        string $deviceType,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO devices (device_id, public_key, auth_token, device_type, created_at, last_seen_at)
             VALUES (:id, :key, :token, :type, :now, :now)',
            [
                ':id' => $deviceId,
                ':key' => $publicKey,
                ':token' => $authToken,
                ':type' => $deviceType,
                ':now' => $now,
            ]
        );
    }

    public function updateLastSeen(string $deviceId, int $now): void
    {
        $this->db->execute(
            'UPDATE devices SET last_seen_at = :now WHERE device_id = :id',
            [':now' => $now, ':id' => $deviceId]
        );
    }

    public function updateFcmToken(string $deviceId, ?string $token): void
    {
        $this->db->execute(
            'UPDATE devices SET fcm_token = :token WHERE device_id = :id',
            [':token' => $token, ':id' => $deviceId]
        );
    }

    /**
     * Record the timestamp of a successful FCM push (accepted by Google's
     * service). Drives the dashboard's "ready 12s ago" indicator so
     * operators can distinguish "token registered but pushes failing" from
     * "token registered and actively working".
     */
    public function bumpFcmLastSuccessAt(string $deviceId, int $when): void
    {
        $this->db->execute(
            'UPDATE devices SET fcm_last_success_at = :when WHERE device_id = :id',
            [':when' => $when, ':id' => $deviceId]
        );
    }

    /**
     * Returns the raw token string or null if the device doesn't exist or
     * hasn't registered a token. Callers that use `empty()` on the return
     * value behave unchanged.
     */
    public function findFcmToken(string $deviceId): ?string
    {
        $row = $this->db->querySingle(
            'SELECT fcm_token FROM devices WHERE device_id = :id',
            [':id' => $deviceId]
        );
        if (!$row || empty($row['fcm_token'])) {
            return null;
        }
        return (string)$row['fcm_token'];
    }

    /**
     * Orders by `last_seen_at DESC` — preserves the dashboard's "most
     * recently active at top" behavior.
     */
    public function findAll(): array
    {
        return $this->db->queryAll('SELECT * FROM devices ORDER BY last_seen_at DESC');
    }
}
