<?php

/**
 * Centralizes X-Device-ID + Bearer token validation and the `last_seen_at`
 * bump that ping/pong liveness depends on (see CLAUDE.md "Liveness probe").
 *
 * Replaces Router::authenticate() and the ad-hoc heartbeat check that
 * used to live inline in DeviceController::health.
 */
class AuthService
{
    /**
     * Enforce authentication. Throws UnauthorizedError on any failure so
     * callers can write linear code.
     */
    public static function requireAuth(Database $db): AuthIdentity
    {
        $identity = self::optional($db);
        if ($identity === null) {
            // Distinguish "no credentials at all" from "bad credentials",
            // mirroring Router::authenticate()'s old behavior.
            $deviceId = $_SERVER['HTTP_X_DEVICE_ID'] ?? null;
            $authHeader = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
            if (!$deviceId || !str_starts_with($authHeader, 'Bearer ')) {
                throw new UnauthorizedError('Missing authentication');
            }
            throw new UnauthorizedError('Invalid credentials');
        }
        return $identity;
    }

    /**
     * Verify credentials if present; return null (not an error) when any
     * header is missing or credentials don't match. Used by the health
     * endpoint, which is public but doubles as a heartbeat when auth
     * headers are supplied.
     *
     * Always bumps `last_seen_at` on a successful lookup.
     */
    public static function optional(Database $db): ?AuthIdentity
    {
        $deviceId = $_SERVER['HTTP_X_DEVICE_ID'] ?? null;
        $authHeader = $_SERVER['HTTP_AUTHORIZATION'] ?? '';

        if (!$deviceId || !str_starts_with($authHeader, 'Bearer ')) {
            return null;
        }

        $token = substr($authHeader, 7);
        $devices = new DeviceRepository($db);
        if (!$devices->findByCredentials($deviceId, $token)) {
            return null;
        }

        $devices->updateLastSeen($deviceId, time());

        return new AuthIdentity($deviceId);
    }
}
