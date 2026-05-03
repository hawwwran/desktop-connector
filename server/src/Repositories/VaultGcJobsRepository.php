<?php

/**
 * Owns SQL touching `vault_gc_jobs`. The same table backs both:
 *
 *   - Ephemeral GC plans (kind ∈ {'sync_plan', 'expiry_plan'}) — short
 *     TTL, executed almost immediately, used by §6.12-§6.13 of
 *     vault-v1.md to coordinate sync-driven and eviction-driven
 *     deletion plans.
 *
 *   - Scheduled hard-purge jobs (kind = 'scheduled_purge') — 24-hour
 *     delay per T14, requires `purge_secret` on execute, cancellable
 *     until executed.
 *
 * One repo, one row per plan/job, distinguished by `kind`. Caller
 * decides the kind at create-time.
 */
class VaultGcJobsRepository
{
    public const STATE_PLANNED   = 'planned';
    public const STATE_EXECUTING = 'executing';
    public const STATE_COMPLETED = 'completed';
    public const STATE_CANCELLED = 'cancelled';
    public const STATE_EXPIRED   = 'expired';
    public const STATE_FAILED    = 'failed';

    public const KIND_SYNC_PLAN       = 'sync_plan';
    public const KIND_EXPIRY_PLAN     = 'expiry_plan';
    public const KIND_SCHEDULED_PURGE = 'scheduled_purge';

    public function __construct(private Database $db) {}

    /**
     * Create a new plan/job row. `targetChunkIds` is stored as a JSON array
     * because the payload is opaque-to-SQL — callers handle membership
     * with PHP-side decode; SQLite has no array type.
     *
     * @param string[] $targetChunkIds
     */
    public function create(
        string $jobId,
        string $vaultId,
        string $kind,
        array $targetChunkIds,
        ?int $scheduledFor,
        int $expiresAt,
        string $requestedByDeviceId,
        int $now
    ): void {
        $this->db->execute(
            'INSERT INTO vault_gc_jobs (
                job_id, vault_id, kind, state, target_chunk_ids,
                scheduled_for, expires_at, requested_by_device_id, created_at
             ) VALUES (
                :id, :vid, :kind, :state, :targets, :sched, :expires, :dev, :now
             )',
            [
                ':id'      => $jobId,
                ':vid'     => $vaultId,
                ':kind'    => $kind,
                ':state'   => self::STATE_PLANNED,
                ':targets' => json_encode(array_values($targetChunkIds)),
                ':sched'   => $scheduledFor,
                ':expires' => $expiresAt,
                ':dev'     => $requestedByDeviceId,
                ':now'     => $now,
            ]
        );
    }

    /** Returns the row decoded with target_chunk_ids re-hydrated as an array. */
    public function getById(string $jobId): ?array
    {
        $row = $this->db->querySingle(
            'SELECT job_id, vault_id, kind, state, target_chunk_ids,
                    scheduled_for, expires_at, started_at, completed_at,
                    cancelled_at, deleted_count, freed_bytes,
                    requested_by_device_id, created_at
             FROM vault_gc_jobs
             WHERE job_id = :id',
            [':id' => $jobId]
        );
        if ($row === null) {
            return null;
        }
        $row['target_chunk_ids'] = json_decode((string)$row['target_chunk_ids'], true) ?: [];
        return $row;
    }

    /**
     * Promote a plan to "completed" with execution counts. Atomic: the
     * UPDATE filters on `state IN (planned, executing)` so a cancelled
     * or already-completed job can't be re-marked.
     */
    public function markCompleted(
        string $jobId,
        int $deletedCount,
        int $freedBytes,
        int $now
    ): bool {
        $this->db->execute(
            'UPDATE vault_gc_jobs
             SET state         = :state,
                 completed_at  = :now,
                 deleted_count = :deleted,
                 freed_bytes   = :freed
             WHERE job_id = :id
               AND state IN (:planned, :executing)',
            [
                ':state'     => self::STATE_COMPLETED,
                ':now'       => $now,
                ':deleted'   => $deletedCount,
                ':freed'     => $freedBytes,
                ':id'        => $jobId,
                ':planned'   => self::STATE_PLANNED,
                ':executing' => self::STATE_EXECUTING,
            ]
        );
        return $this->db->changes() === 1;
    }

    /**
     * Cancel a plan/job. Idempotent: cancelling an already-cancelled or
     * already-completed plan is a no-op (returns false, but caller
     * shouldn't care — the wire endpoint always returns 204).
     */
    public function markCancelled(string $jobId, int $now): bool
    {
        $this->db->execute(
            'UPDATE vault_gc_jobs
             SET state        = :state,
                 cancelled_at = :now
             WHERE job_id = :id
               AND state IN (:planned, :executing)',
            [
                ':state'     => self::STATE_CANCELLED,
                ':now'       => $now,
                ':id'        => $jobId,
                ':planned'   => self::STATE_PLANNED,
                ':executing' => self::STATE_EXECUTING,
            ]
        );
        return $this->db->changes() === 1;
    }
}
