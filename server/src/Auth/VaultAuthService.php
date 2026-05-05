<?php

/**
 * Vault-specific authentication. Sits on top of the existing AuthService
 * and adds the second-credential check that vault endpoints require:
 * X-Vault-Authorization: Bearer <secret> matched against the per-vault
 * `vault_access_token_hash` stored at vault create.
 *
 * Per T0 §A5 + the wire spec at vault-v1.md §2:
 *
 *   - Failure of device auth (X-Device-ID / Authorization) surfaces as
 *     vault_auth_failed with `details.kind = "device"` — the client
 *     prompts to re-pair.
 *   - Failure of vault auth (X-Vault-Authorization) surfaces as
 *     vault_auth_failed with `details.kind = "vault"` — the client
 *     prompts to re-enter the vault unlock or re-grant.
 *
 * The vault access secret is a high-entropy bearer capability separate
 * from the Vault Master Key. The relay only ever sees its hash; the
 * secret stays client-side.
 */
class VaultAuthService
{
    /**
     * Role rank table (T0 §D11). Higher rank == strictly more privileges.
     * Vault endpoints use ``requireRole`` to gate writes by minimum role;
     * a caller with a higher rank automatically passes a lower-rank check.
     */
    private const ROLE_RANK = [
        'read-only'      => 1,
        'browse-upload'  => 2,
        'sync'           => 3,
        'admin'          => 4,
    ];

    /**
     * Validate device + vault auth and confirm the vault exists. Returns
     * the matched vault row on success so the caller doesn't need a
     * second SELECT. Throws on every failure so callers stay linear:
     *
     *   $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
     *
     * @return array The full `vaults` row (per VaultsRepository::getById).
     */
    public static function requireVaultAuth(Database $db, string $vaultId): array
    {
        // 1. Device auth. AuthService::requireAuth throws UnauthorizedError
        //    on legacy shape; vault endpoints want the vault_v1 envelope, so
        //    we translate.
        try {
            AuthService::requireAuth($db);
        } catch (UnauthorizedError $e) {
            throw new VaultAuthFailedError('device');
        }

        // 2. The header-vs-path vault id sanity check (vault-v1.md §2).
        //    The redundancy is intentional: it catches client bugs that
        //    would otherwise leak across vaults.
        $headerVaultId = $_SERVER['HTTP_X_VAULT_ID'] ?? null;
        if ($headerVaultId !== null && $headerVaultId !== $vaultId) {
            throw new VaultInvalidRequestError(
                'X-Vault-ID header does not match path vault_id',
                'vault_id'
            );
        }

        // 3. X-Vault-Authorization: Bearer <secret>.
        $vaultAuthHeader = $_SERVER['HTTP_X_VAULT_AUTHORIZATION'] ?? '';
        if (!is_string($vaultAuthHeader) || !str_starts_with($vaultAuthHeader, 'Bearer ')) {
            throw new VaultAuthFailedError('vault');
        }
        $secret = substr($vaultAuthHeader, 7);
        if ($secret === '') {
            throw new VaultAuthFailedError('vault');
        }

        // 4. Existence + bearer match. Look up the vault row first so we
        //    can return it on success, then constant-time-compare the
        //    secret hash. Order matters: an unknown vault yields
        //    vault_not_found (404) so a typo in the path doesn't masquerade
        //    as a 401 the user thinks is a credential problem.
        $vaultsRepo = new VaultsRepository($db);
        $vault = $vaultsRepo->getById($vaultId);
        if ($vault === null) {
            throw new VaultNotFoundError($vaultId);
        }

        $expectedHash = (string)$vault['vault_access_token_hash'];
        $actualHash   = hash('sha256', $secret, true);   // raw 32 bytes
        if (!hash_equals($expectedHash, $actualHash)) {
            throw new VaultAuthFailedError('vault');
        }

        return $vault;
    }

    /**
     * Helper for `POST /api/vaults` — the create endpoint requires device
     * auth but the vault doesn't exist yet (it's being created). Mirrors
     * the device-only path of requireVaultAuth without the vault-bearer
     * check. Raises vault_auth_failed(kind=device) on failure.
     *
     * Returns the device id from the auth identity.
     */
    public static function requireDeviceAuthForCreate(Database $db): string
    {
        try {
            $identity = AuthService::requireAuth($db);
        } catch (UnauthorizedError $e) {
            throw new VaultAuthFailedError('device');
        }
        return $identity->deviceId;
    }

    /**
     * Enforce the §D11 role matrix on a vault endpoint. Call this AFTER
     * ``requireVaultAuth`` so device + vault credentials are already
     * validated. Throws ``VaultAccessDeniedError(required_role=$minRole)``
     * if the caller has no grant, has a revoked grant, or has a grant
     * with role rank below ``$minRole``. Returns the matched grant row.
     *
     * @return array vault_device_grants row (per ::getByDevice).
     */
    public static function requireRole(
        Database $db,
        string $vaultId,
        string $deviceId,
        string $minRole
    ): array {
        $required = self::ROLE_RANK[$minRole] ?? null;
        if ($required === null) {
            throw new InvalidArgumentException("unknown role: {$minRole}");
        }
        $grants = new VaultDeviceGrantsRepository($db);
        $grant = $grants->getByDevice($vaultId, $deviceId);
        if ($grant === null) {
            throw new VaultAccessDeniedError(
                'caller is not a granted device on this vault',
                requiredRole: $minRole,
            );
        }
        if ($grant['revoked_at'] !== null) {
            throw new VaultAccessDeniedError(
                'grant has been revoked',
                requiredRole: $minRole,
            );
        }
        $callerRole = (string)$grant['role'];
        $have = self::ROLE_RANK[$callerRole] ?? 0;
        if ($have < $required) {
            throw new VaultAccessDeniedError(
                "operation requires role={$minRole}, caller has role={$callerRole}",
                requiredRole: $minRole,
            );
        }
        return $grant;
    }

    /**
     * Resolve the caller's device id from the X-Device-ID header. Vault
     * endpoints already validated the credentials in ``requireVaultAuth``;
     * this is the small helper that lets controllers pass the id into
     * ``requireRole`` without repeating the $_SERVER lookup.
     */
    public static function callerDeviceId(): string
    {
        return (string)($_SERVER['HTTP_X_DEVICE_ID'] ?? '');
    }
}
