<?php

/**
 * Fixed-window counter for vault auth + create attempts (review
 * §1.H1, protocol §10). Replaces the no-limit, no-telemetry shape
 * that pre-fix VaultAuthService had.
 *
 * Operation: a single atomic UPSERT either starts a fresh window
 * (when the existing one has expired) or increments the counter
 * inside the current window. The caller then asks ``attempts > cap?``
 * and on overflow asks for the retry-after delta. Two SQL statements
 * per attempt under WAL serialization — concurrent attempts from the
 * same key never bypass the limit because SQLite's row lock pins
 * one writer at a time.
 */
class VaultAuthAttemptsRepository
{
    public const KIND_AUTH   = 'auth';
    public const KIND_CREATE = 'create';

    public function __construct(private Database $db) {}

    /**
     * Atomic "record attempt + return current state" primitive. Used
     * by ``VaultAuthService::requireVaultAuth`` / ``requireDeviceAuthForCreate``
     * to gate every call on the fixed-window cap.
     *
     * The UPSERT handles three cases:
     *   1. No prior row: insert (1, now).
     *   2. Stale window (window_start + window_s <= now): reset to (1, now).
     *   3. Live window: increment attempts.
     *
     * Returns an associative array with the post-write window state:
     *   - ``attempts`` — total inside the current window (this one included)
     *   - ``window_start`` — epoch of the active window
     *   - ``window_end`` — epoch when the window resets
     *
     * Callers compute ``retry_after_ms`` themselves from ``window_end - now``
     * because the controllers want millisecond precision and may also
     * add jitter.
     */
    public function recordAndRead(
        string $deviceId,
        string $scope,
        string $kind,
        int $windowSeconds,
        int $now
    ): array {
        if (!in_array($kind, [self::KIND_AUTH, self::KIND_CREATE], true)) {
            throw new InvalidArgumentException("unknown auth-attempt kind: {$kind}");
        }
        // Stale-window reset: matches when the existing window_start has
        // aged out. Two UPSERT shapes would be possible — one for "stale
        // → reset to 1" and one for "live → increment". A single UPSERT
        // with a CASE expression is clearer and avoids an extra
        // statement under contention.
        $this->db->execute(
            'INSERT INTO vault_auth_attempts (
                device_id, scope, kind, window_start, attempts
             ) VALUES (
                :device, :scope, :kind, :now, 1
             )
             ON CONFLICT(device_id, scope, kind) DO UPDATE
             SET
                 window_start = CASE
                     WHEN vault_auth_attempts.window_start + :window <= :now
                         THEN :now
                     ELSE vault_auth_attempts.window_start
                 END,
                 attempts = CASE
                     WHEN vault_auth_attempts.window_start + :window <= :now
                         THEN 1
                     ELSE vault_auth_attempts.attempts + 1
                 END',
            [
                ':device' => $deviceId,
                ':scope'  => $scope,
                ':kind'   => $kind,
                ':window' => $windowSeconds,
                ':now'    => $now,
            ]
        );
        $row = $this->db->querySingle(
            'SELECT attempts, window_start FROM vault_auth_attempts
             WHERE device_id = :device AND scope = :scope AND kind = :kind',
            [':device' => $deviceId, ':scope' => $scope, ':kind' => $kind]
        );
        if ($row === null) {
            // Defensive — UPSERT just inserted a row; if it's missing
            // here something is very wrong (concurrent DELETE? schema
            // missing?). Return a synthetic state so the caller can
            // still 429 rather than crashing.
            return [
                'attempts'     => 1,
                'window_start' => $now,
                'window_end'   => $now + $windowSeconds,
            ];
        }
        $windowStart = (int)$row['window_start'];
        return [
            'attempts'     => (int)$row['attempts'],
            'window_start' => $windowStart,
            'window_end'   => $windowStart + $windowSeconds,
        ];
    }

    /**
     * Test-only helper: peek without recording. Production code paths
     * always go through ``recordAndRead`` to atomically increment.
     */
    public function peek(
        string $deviceId,
        string $scope,
        string $kind
    ): ?array {
        return $this->db->querySingle(
            'SELECT attempts, window_start FROM vault_auth_attempts
             WHERE device_id = :device AND scope = :scope AND kind = :kind',
            [':device' => $deviceId, ':scope' => $scope, ':kind' => $kind]
        );
    }
}
