<?php

/**
 * vault_join_requests queries (T13.1 / vault-v1.md §8).
 *
 * Lifecycle: ``pending`` (admin posted) → ``claimed`` (claimant device posted
 * its pubkey + name) → ``approved`` (admin wrapped a vault grant for the
 * claimant) | ``rejected`` (admin declined) | ``expired`` (15 min default).
 *
 * Fields the wire surface needs:
 *   - join_request_id (jr_v1_<24base32>) — handed to the QR encoder.
 *   - ephemeral_admin_pubkey — 32-byte X25519 (admin's session keypair).
 *   - claimant_pubkey + device_name — set on claim; the verification code
 *     is derived deterministically off both pubkeys client-side.
 *   - approved_role + wrapped_vault_grant — set on approve; the claimant
 *     downloads these via GET /api/vaults/{id}/join-requests/{req_id} and
 *     unwraps with their ephemeral private key.
 *   - expires_at — 15 minutes after creation per §A14.
 */

class VaultJoinRequestsRepository
{
    public function __construct(private Database $db) {}

    public function create(
        string $joinRequestId,
        string $vaultId,
        string $ephemeralAdminPubkey,
        int $createdAt,
        int $expiresAt
    ): void {
        $this->db->execute(
            'INSERT INTO vault_join_requests (
                join_request_id, vault_id, state, ephemeral_admin_pubkey,
                expires_at, created_at
             ) VALUES (
                :id, :vault_id, :state, :pubkey, :expires_at, :created_at
             )',
            [
                ':id'         => $joinRequestId,
                ':vault_id'   => $vaultId,
                ':state'      => 'pending',
                ':pubkey'     => new Blob($ephemeralAdminPubkey),
                ':expires_at' => $expiresAt,
                ':created_at' => $createdAt,
            ]
        );
    }

    public function get(string $joinRequestId): ?array
    {
        $row = $this->db->querySingle(
            'SELECT join_request_id, vault_id, state, ephemeral_admin_pubkey,
                    claimant_device_id, claimant_pubkey, device_name,
                    approved_role, wrapped_vault_grant, granted_by_device_id,
                    expires_at, created_at, claimed_at, approved_at, rejected_at
             FROM vault_join_requests
             WHERE join_request_id = :id',
            [':id' => $joinRequestId]
        );
        return $row ?: null;
    }

    /**
     * Mark expired rows whose expires_at is in the past — called from the
     * controller before serving any join-request endpoint so a stale row
     * gets cleaned up rather than silently rejected.
     */
    public function expirePastDue(int $now): int
    {
        $this->db->execute(
            "UPDATE vault_join_requests
                SET state = 'expired'
              WHERE state IN ('pending', 'claimed')
                AND expires_at <= :now",
            [':now' => $now]
        );
        return $this->db->changes();
    }

    /**
     * Atomic claim: only flips a ``pending`` row, captures the claimant
     * pubkey + device_name + claimed_at. Returns the post-claim row, or
     * ``null`` if the row wasn't claimable (already claimed, expired, or
     * unknown).
     */
    public function claim(
        string $joinRequestId,
        string $claimantDeviceId,
        string $claimantPubkey,
        string $deviceName,
        int $now
    ): ?array {
        $this->db->execute(
            "UPDATE vault_join_requests
                SET state = 'claimed',
                    claimant_device_id = :device_id,
                    claimant_pubkey = :pubkey,
                    device_name = :name,
                    claimed_at = :now
              WHERE join_request_id = :id
                AND state = 'pending'",
            [
                ':id'        => $joinRequestId,
                ':device_id' => $claimantDeviceId,
                ':pubkey'    => new Blob($claimantPubkey),
                ':name'      => $deviceName,
                ':now'       => $now,
            ]
        );
        if ($this->db->changes() !== 1) {
            return null;
        }
        return $this->get($joinRequestId);
    }

    /**
     * Atomic approve: only flips a ``claimed`` row, attaches the wrapped
     * grant and approver. Returns the post-approve row or ``null`` if the
     * row wasn't approvable.
     */
    public function approve(
        string $joinRequestId,
        string $approvedRole,
        string $wrappedVaultGrant,
        string $grantedByDeviceId,
        int $now
    ): ?array {
        $this->db->execute(
            "UPDATE vault_join_requests
                SET state = 'approved',
                    approved_role = :role,
                    wrapped_vault_grant = :grant,
                    granted_by_device_id = :granter,
                    approved_at = :now
              WHERE join_request_id = :id
                AND state = 'claimed'",
            [
                ':id'      => $joinRequestId,
                ':role'    => $approvedRole,
                ':grant'   => new Blob($wrappedVaultGrant),
                ':granter' => $grantedByDeviceId,
                ':now'     => $now,
            ]
        );
        if ($this->db->changes() !== 1) {
            return null;
        }
        return $this->get($joinRequestId);
    }

    public function reject(string $joinRequestId, int $now): bool
    {
        $this->db->execute(
            "UPDATE vault_join_requests
                SET state = 'rejected', rejected_at = :now
              WHERE join_request_id = :id
                AND state IN ('pending', 'claimed')",
            [':id' => $joinRequestId, ':now' => $now]
        );
        return $this->db->changes() === 1;
    }
}
