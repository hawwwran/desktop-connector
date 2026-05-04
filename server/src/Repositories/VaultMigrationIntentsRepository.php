<?php
/**
 * vault_migration_intents queries (T9.2 / §H2).
 *
 * Idempotency contract: /migration/start returns the existing record
 * verbatim when called again with the same token, even if the
 * target_relay_url changed (the original target wins so a flapping
 * client doesn't drift the source's intent). All writes return whether
 * a row landed so the controller can choose between 200/201/409.
 */

class VaultMigrationIntentsRepository
{
    /** @var Database */
    private $db;

    public function __construct(Database $db)
    {
        $this->db = $db;
    }

    /**
     * Idempotent insert. Returns the row that ends up persisted —
     * either the freshly-inserted one or the pre-existing record when
     * an intent already exists (the existing token wins so a retried
     * /migration/start with a different generated token doesn't
     * overwrite the one the source already handed out).
     *
     * Returns ['record' => array, 'created' => bool].
     */
    public function recordIntent(
        string $vaultId,
        string $tokenHashBinary,
        string $targetRelayUrl,
        string $initiatingDevice,
        int $now
    ): array {
        $existing = $this->getIntent($vaultId);
        if ($existing !== null) {
            return ['record' => $existing, 'created' => false];
        }
        $this->db->execute(
            'INSERT INTO vault_migration_intents (
                vault_id, token_hash, target_relay_url,
                started_at, initiating_device
             ) VALUES (
                :vault_id, :token_hash, :target,
                :started_at, :device
             )',
            [
                ':vault_id'   => $vaultId,
                ':token_hash' => $tokenHashBinary,
                ':target'     => $targetRelayUrl,
                ':started_at' => $now,
                ':device'     => $initiatingDevice,
            ]
        );
        return [
            'record'  => $this->getIntent($vaultId),
            'created' => true,
        ];
    }

    public function getIntent(string $vaultId): ?array
    {
        return $this->db->querySingle(
            'SELECT vault_id, token_hash, target_relay_url,
                    started_at, verified_at, committed_at,
                    initiating_device
             FROM vault_migration_intents
             WHERE vault_id = :id',
            [':id' => $vaultId]
        );
    }

    public function markVerified(string $vaultId, int $now): bool
    {
        $this->db->execute(
            'UPDATE vault_migration_intents
             SET verified_at = :now
             WHERE vault_id = :id',
            [':now' => $now, ':id' => $vaultId]
        );
        return $this->db->changes() === 1;
    }

    public function markCommitted(string $vaultId, int $now): bool
    {
        $this->db->execute(
            'UPDATE vault_migration_intents
             SET committed_at = :now
             WHERE vault_id = :id',
            [':now' => $now, ':id' => $vaultId]
        );
        return $this->db->changes() === 1;
    }

    public function cancelIntent(string $vaultId): bool
    {
        $this->db->execute(
            'DELETE FROM vault_migration_intents WHERE vault_id = :id',
            [':id' => $vaultId]
        );
        return $this->db->changes() === 1;
    }
}
