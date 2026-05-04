<?php

/**
 * vault_device_grants queries (T13.1, T13.5).
 *
 * One row per (vault_id, device_id) pair. ``revoked_at`` switches a grant
 * off without deleting the audit row — every successful approve, every
 * revoke, every "Revoke and rotate" combo (T13.6) leaves a trail here so
 * the admin device's Devices tab can render history per §gaps §14.
 *
 * The vault auth path consults ``isActiveGrant`` to enforce §D11 role
 * checks; controllers translate the ``role`` column into operation
 * permissions (e.g. only ``admin`` can rotate the access secret, only
 * ``sync``+ can publish manifests).
 */

class VaultDeviceGrantsRepository
{
    public function __construct(private Database $db) {}

    public function insertGrant(
        string $grantId,
        string $vaultId,
        string $deviceId,
        ?string $deviceName,
        string $role,
        string $grantedBy,
        string $grantedVia,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO vault_device_grants (
                grant_id, vault_id, device_id, device_name, role,
                granted_by, granted_via, granted_at
             ) VALUES (
                :id, :vault, :device, :name, :role,
                :by, :via, :now
             )',
            [
                ':id'     => $grantId,
                ':vault'  => $vaultId,
                ':device' => $deviceId,
                ':name'   => $deviceName,
                ':role'   => $role,
                ':by'     => $grantedBy,
                ':via'    => $grantedVia,
                ':now'    => $now,
            ]
        );
    }

    public function listForVault(string $vaultId): array
    {
        $stmt = $this->db->query(
            'SELECT grant_id, vault_id, device_id, device_name, role,
                    granted_by, granted_via, granted_at,
                    revoked_at, revoked_by, last_seen_at
             FROM vault_device_grants
             WHERE vault_id = :vault
             ORDER BY granted_at ASC',
            [':vault' => $vaultId]
        );
        $rows = [];
        while (($r = $stmt->fetchArray(SQLITE3_ASSOC)) !== false) {
            $rows[] = $r;
        }
        return $rows;
    }

    public function getByDevice(string $vaultId, string $deviceId): ?array
    {
        $row = $this->db->querySingle(
            'SELECT grant_id, vault_id, device_id, device_name, role,
                    granted_by, granted_via, granted_at,
                    revoked_at, revoked_by, last_seen_at
             FROM vault_device_grants
             WHERE vault_id = :vault AND device_id = :device',
            [':vault' => $vaultId, ':device' => $deviceId]
        );
        return $row ?: null;
    }

    public function isActiveGrant(string $vaultId, string $deviceId): bool
    {
        $row = $this->getByDevice($vaultId, $deviceId);
        return $row !== null && $row['revoked_at'] === null;
    }

    public function revoke(
        string $vaultId,
        string $deviceId,
        string $revokedBy,
        int $now
    ): bool {
        $this->db->execute(
            'UPDATE vault_device_grants
                SET revoked_at = :now, revoked_by = :by
              WHERE vault_id = :vault AND device_id = :device
                AND revoked_at IS NULL',
            [
                ':vault'  => $vaultId,
                ':device' => $deviceId,
                ':now'    => $now,
                ':by'     => $revokedBy,
            ]
        );
        return $this->db->changes() === 1;
    }

    public function bumpLastSeen(string $vaultId, string $deviceId, int $now): void
    {
        $this->db->execute(
            'UPDATE vault_device_grants
                SET last_seen_at = :now
              WHERE vault_id = :vault AND device_id = :device
                AND revoked_at IS NULL',
            [':vault' => $vaultId, ':device' => $deviceId, ':now' => $now]
        );
    }

    public function recordRotation(
        string $vaultId,
        string $rotatedBy,
        int $now,
        ?string $triggeredByRevokeGrantId = null
    ): void {
        $this->db->execute(
            'INSERT INTO vault_access_secret_rotations (
                vault_id, rotated_at, rotated_by, triggered_by_revoke_grant_id
             ) VALUES (
                :vault, :now, :by, :trigger
             )',
            [
                ':vault'   => $vaultId,
                ':now'     => $now,
                ':by'      => $rotatedBy,
                ':trigger' => $triggeredByRevokeGrantId,
            ]
        );
    }
}
