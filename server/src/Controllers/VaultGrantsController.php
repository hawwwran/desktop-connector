<?php

/**
 * QR-assisted vault-grant + revocation + access-secret-rotation HTTP surface
 * (T13.1, vault-v1.md §8). Mirrors the VaultController pattern: one static
 * method per endpoint, signature
 *
 *   (Database $db, RequestContext $ctx)
 *
 * Auth: every endpoint goes through ``VaultAuthService::requireVaultAuth``.
 * For the admin-side endpoints (createJoinRequest / approve / reject /
 * revokeDeviceGrant / rotateAccessSecret) the caller must additionally
 * hold an ``admin`` grant — the controller checks and 403s with
 * ``vault_access_denied`` if not. The claimant-side endpoints (claim,
 * pollJoinRequest) are unauthenticated against the vault by design — the
 * claimant is the one *trying* to gain access — but they must still hold
 * a registered device identity (``X-Device-ID`` header), and the QR-encoded
 * join_request_id is the bearer authorisation.
 *
 * Note (T13.1 — server scope): the wire surface here doesn't yet enforce
 * the role matrix beyond admin/non-admin. Per-role write gates land in
 * the affected controllers (e.g. only ``sync``+ may PUT a manifest); that
 * happens in a follow-up so the data layer + endpoints land first.
 */
class VaultGrantsController
{
    private const JOIN_REQUEST_TTL_S = 15 * 60; // 15 minutes (T13.2)

    /** Server-side timestamp serialization (UTC, second precision, RFC 3339). */
    private static function ts(int $epoch): string
    {
        return gmdate('Y-m-d\TH:i:s\Z', $epoch);
    }

    /** Internal canonical (undashed, uppercase) → display form (4-4-4 dashed). */
    private static function dashedVaultId(string $undashed): string
    {
        return substr($undashed, 0, 4) . '-' . substr($undashed, 4, 4) . '-' . substr($undashed, 8, 4);
    }

    /** Strip dashes, uppercase, validate. Throws on malformed id. */
    private static function normalizeVaultId(string $any): string
    {
        $stripped = strtoupper(str_replace('-', '', $any));
        if (!preg_match('/^[A-Z2-7]{12}$/', $stripped)) {
            throw new VaultInvalidRequestError('vault_id must be 12 base32 characters', 'vault_id');
        }
        return $stripped;
    }

    /** base64-decode strict + length check. Throws VaultInvalidRequestError on either failure. */
    private static function decodeBase64Field(array $body, string $field, ?int $expectedLength = null): string
    {
        $b64 = Validators::requireNonEmptyString($body, $field);
        $raw = base64_decode($b64, true);
        if ($raw === false) {
            throw new VaultInvalidRequestError("{$field} is not valid base64", $field);
        }
        if ($expectedLength !== null && strlen($raw) !== $expectedLength) {
            throw new VaultInvalidRequestError(
                "{$field} must decode to {$expectedLength} bytes",
                $field
            );
        }
        return $raw;
    }

    private static function deviceIdHeader(): string
    {
        $deviceId = (string)($_SERVER['HTTP_X_DEVICE_ID'] ?? '');
        if ($deviceId === '') {
            throw new VaultInvalidRequestError('Missing X-Device-ID header', 'X-Device-ID');
        }
        return $deviceId;
    }

    private static function requireAdmin(Database $db, string $vaultId, string $deviceId): void
    {
        $grants = new VaultDeviceGrantsRepository($db);
        $grant = $grants->getByDevice($vaultId, $deviceId);
        if ($grant === null || $grant['revoked_at'] !== null) {
            throw new VaultAccessDeniedError('caller is not a granted device on this vault');
        }
        if ((string)$grant['role'] !== 'admin') {
            throw new VaultAccessDeniedError(
                "operation requires role=admin, caller has role={$grant['role']}"
            );
        }
    }

    private static function generateId(string $prefix): string
    {
        $alphabet = 'abcdefghijklmnopqrstuvwxyz234567';
        $raw = random_bytes(15);
        $bits = 0;
        $buf = 0;
        $out = '';
        for ($i = 0; $i < strlen($raw); $i++) {
            $buf = ($buf << 8) | ord($raw[$i]);
            $bits += 8;
            while ($bits >= 5) {
                $bits -= 5;
                $out .= $alphabet[($buf >> $bits) & 0x1f];
            }
        }
        return $prefix . substr($out, 0, 24);
    }

    private static function joinRequestPayload(array $row, bool $includeWrappedGrant): array
    {
        $payload = [
            'join_request_id'        => $row['join_request_id'],
            'vault_id'               => self::dashedVaultId((string)$row['vault_id']),
            'state'                  => $row['state'],
            'ephemeral_admin_pubkey' => base64_encode((string)$row['ephemeral_admin_pubkey']),
            'expires_at'             => self::ts((int)$row['expires_at']),
            'created_at'              => self::ts((int)$row['created_at']),
            'claimed_at'              => isset($row['claimed_at']) ? self::ts((int)$row['claimed_at']) : null,
            'approved_at'             => isset($row['approved_at']) ? self::ts((int)$row['approved_at']) : null,
            'rejected_at'             => isset($row['rejected_at']) ? self::ts((int)$row['rejected_at']) : null,
            'claimant_device_id'      => $row['claimant_device_id'] ?: null,
            'claimant_pubkey'         => $row['claimant_pubkey']
                ? base64_encode((string)$row['claimant_pubkey']) : null,
            'device_name'             => $row['device_name'] ?: null,
            'approved_role'           => $row['approved_role'] ?: null,
            'granted_by_device_id'    => $row['granted_by_device_id'] ?: null,
        ];
        if ($includeWrappedGrant && $row['wrapped_vault_grant']) {
            $payload['wrapped_vault_grant'] = base64_encode((string)$row['wrapped_vault_grant']);
        } else {
            $payload['wrapped_vault_grant'] = null;
        }
        return $payload;
    }

    // ===================================================================
    //  POST /api/vaults/{vault_id}/join-requests              (T13.1 / 13.2)
    // ===================================================================

    public static function createJoinRequest(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);
        $deviceId = self::deviceIdHeader();
        self::requireAdmin($db, $vaultId, $deviceId);

        $body = $ctx->jsonBody();
        $pubkey = self::decodeBase64Field($body, 'ephemeral_admin_pubkey', 32);

        $repo = new VaultJoinRequestsRepository($db);
        $now = time();
        $repo->expirePastDue($now);
        $joinRequestId = self::generateId('jr_v1_');
        $repo->create(
            $joinRequestId, $vaultId, $pubkey, $now, $now + self::JOIN_REQUEST_TTL_S
        );
        $row = $repo->get($joinRequestId);

        Router::json([
            'ok' => true,
            'data' => self::joinRequestPayload($row, includeWrappedGrant: false),
        ], 201);
    }

    // ===================================================================
    //  GET /api/vaults/{vault_id}/join-requests/{req_id}      (T13.3 poll)
    // ===================================================================

    public static function getJoinRequest(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);
        $reqId = (string)($ctx->params['req_id'] ?? '');
        if (!preg_match('/^jr_v1_[a-z2-7]{24}$/', $reqId)) {
            throw new VaultInvalidRequestError('malformed join_request_id', 'req_id');
        }

        $repo = new VaultJoinRequestsRepository($db);
        $repo->expirePastDue(time());
        $row = $repo->get($reqId);
        if ($row === null || (string)$row['vault_id'] !== $vaultId) {
            throw new VaultJoinRequestStateError("unknown join-request: {$reqId}");
        }

        // The wrapped grant is *only* visible to the claimant device — it
        // contains the AEAD-wrapped vault material. Other callers see the
        // metadata only.
        $callerDevice = (string)($_SERVER['HTTP_X_DEVICE_ID'] ?? '');
        $includeGrant = ($row['claimant_device_id'] ?? '') === $callerDevice
            && $row['state'] === 'approved';

        Router::json([
            'ok' => true,
            'data' => self::joinRequestPayload($row, includeWrappedGrant: $includeGrant),
        ], 200);
    }

    // ===================================================================
    //  POST /api/vaults/{vault_id}/join-requests/{req_id}/claim (T13.3)
    // ===================================================================

    public static function claim(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);
        $reqId = (string)($ctx->params['req_id'] ?? '');
        if (!preg_match('/^jr_v1_[a-z2-7]{24}$/', $reqId)) {
            throw new VaultInvalidRequestError('malformed join_request_id', 'req_id');
        }
        $deviceId = self::deviceIdHeader();

        $body = $ctx->jsonBody();
        $pubkey = self::decodeBase64Field($body, 'claimant_pubkey', 32);
        $deviceName = isset($body['device_name'])
            ? (string)$body['device_name'] : '';

        $repo = new VaultJoinRequestsRepository($db);
        $repo->expirePastDue(time());
        $existing = $repo->get($reqId);
        if ($existing === null || (string)$existing['vault_id'] !== $vaultId) {
            throw new VaultJoinRequestStateError("unknown join-request: {$reqId}");
        }
        if ((string)$existing['state'] !== 'pending') {
            throw new VaultJoinRequestStateError(
                "join-request not in pending state: {$existing['state']}", 409
            );
        }

        $row = $repo->claim($reqId, $deviceId, $pubkey, $deviceName, time());
        if ($row === null) {
            throw new VaultJoinRequestStateError(
                'join-request was claimed concurrently', 409
            );
        }

        Router::json([
            'ok' => true,
            'data' => self::joinRequestPayload($row, includeWrappedGrant: false),
        ], 200);
    }

    // ===================================================================
    //  POST /api/vaults/{vault_id}/join-requests/{req_id}/approve (T13.4)
    // ===================================================================

    public static function approve(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);
        $reqId = (string)($ctx->params['req_id'] ?? '');
        if (!preg_match('/^jr_v1_[a-z2-7]{24}$/', $reqId)) {
            throw new VaultInvalidRequestError('malformed join_request_id', 'req_id');
        }
        $approver = self::deviceIdHeader();
        self::requireAdmin($db, $vaultId, $approver);

        $body = $ctx->jsonBody();
        $role = Validators::requireNonEmptyString($body, 'approved_role');
        if (!in_array($role, ['read-only', 'browse-upload', 'sync', 'admin'], true)) {
            throw new VaultInvalidRequestError(
                "approved_role must be one of read-only|browse-upload|sync|admin (got {$role})",
                'approved_role'
            );
        }
        $wrapped = self::decodeBase64Field($body, 'wrapped_vault_grant');

        $repo = new VaultJoinRequestsRepository($db);
        $grants = new VaultDeviceGrantsRepository($db);
        $now = time();
        $repo->expirePastDue($now);

        $existing = $repo->get($reqId);
        if ($existing === null || (string)$existing['vault_id'] !== $vaultId) {
            throw new VaultJoinRequestStateError("unknown join-request: {$reqId}");
        }
        if ((string)$existing['state'] !== 'claimed') {
            throw new VaultJoinRequestStateError(
                "join-request not in claimed state: {$existing['state']}", 409
            );
        }

        $row = $repo->approve($reqId, $role, $wrapped, $approver, $now);
        if ($row === null) {
            throw new VaultJoinRequestStateError(
                'join-request was approved concurrently', 409
            );
        }

        // Insert the post-approval grant so subsequent vault calls from
        // the claimant device pass the role gate.
        $grantId = self::generateId('dg_v1_');
        $grants->insertGrant(
            $grantId,
            $vaultId,
            (string)$row['claimant_device_id'],
            (string)($row['device_name'] ?: null),
            $role,
            $approver,
            'qr',
            $now
        );

        Router::json([
            'ok' => true,
            'data' => self::joinRequestPayload($row, includeWrappedGrant: false),
        ], 200);
    }

    // ===================================================================
    //  DELETE /api/vaults/{vault_id}/device-grants/{device_id}    (T13.5)
    // ===================================================================

    public static function revokeDeviceGrant(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);
        $caller = self::deviceIdHeader();
        self::requireAdmin($db, $vaultId, $caller);

        $target = (string)($ctx->params['device_id'] ?? '');
        if (!preg_match('/^[a-f0-9]{32}$/', $target)) {
            throw new VaultInvalidRequestError('device_id must be 32 lowercase hex chars', 'device_id');
        }
        if ($target === $caller) {
            throw new VaultInvalidRequestError(
                'admin cannot revoke their own grant; transfer admin role first', 'device_id'
            );
        }

        $grants = new VaultDeviceGrantsRepository($db);
        $now = time();
        $existing = $grants->getByDevice($vaultId, $target);
        if ($existing === null) {
            throw new VaultJoinRequestStateError("no grant for device {$target}");
        }
        if ($existing['revoked_at'] !== null) {
            // Idempotent: already revoked.
            Router::json([
                'ok' => true,
                'data' => [
                    'vault_id'   => self::dashedVaultId($vaultId),
                    'device_id'  => $target,
                    'revoked_at' => self::ts((int)$existing['revoked_at']),
                    'already_revoked' => true,
                ],
            ], 200);
            return;
        }

        $grants->revoke($vaultId, $target, $caller, $now);
        Router::json([
            'ok' => true,
            'data' => [
                'vault_id'   => self::dashedVaultId($vaultId),
                'device_id'  => $target,
                'revoked_at' => self::ts($now),
                'already_revoked' => false,
            ],
        ], 200);
    }

    // ===================================================================
    //  GET /api/vaults/{vault_id}/device-grants                  (T13.5)
    // ===================================================================

    /**
     * List every device that has been granted access to the vault — the
     * Devices tab in Vault settings reads this. Active + revoked rows
     * both appear so the admin can see history; the UI greys out revoked
     * rows.
     */
    public static function listDeviceGrants(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);
        $caller = self::deviceIdHeader();
        self::requireAdmin($db, $vaultId, $caller);

        $grants = new VaultDeviceGrantsRepository($db);
        $rows = $grants->listForVault($vaultId);
        $payload = [];
        foreach ($rows as $row) {
            $payload[] = [
                'grant_id'      => (string)$row['grant_id'],
                'device_id'     => (string)$row['device_id'],
                'device_name'   => $row['device_name'] !== null ? (string)$row['device_name'] : null,
                'role'          => (string)$row['role'],
                'granted_by'    => (string)$row['granted_by'],
                'granted_via'   => (string)$row['granted_via'],
                'granted_at'    => self::ts((int)$row['granted_at']),
                'revoked_at'    => $row['revoked_at'] !== null
                    ? self::ts((int)$row['revoked_at']) : null,
                'revoked_by'    => $row['revoked_by'] !== null
                    ? (string)$row['revoked_by'] : null,
                'last_seen_at'  => $row['last_seen_at'] !== null
                    ? self::ts((int)$row['last_seen_at']) : null,
                'is_revoked'    => $row['revoked_at'] !== null,
                'is_caller'     => (string)$row['device_id'] === $caller,
            ];
        }

        Router::json([
            'ok' => true,
            'data' => [
                'vault_id' => self::dashedVaultId($vaultId),
                'grants'   => $payload,
            ],
        ], 200);
    }

    // ===================================================================
    //  POST /api/vaults/{vault_id}/access-secret/rotate          (T13.6)
    // ===================================================================

    public static function rotateAccessSecret(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);
        $caller = self::deviceIdHeader();
        self::requireAdmin($db, $vaultId, $caller);

        $body = $ctx->jsonBody();
        // Old secret is implicit in the auth header; new secret hash is
        // posted in the body so the relay never sees the plaintext.
        $newHash = self::decodeBase64Field($body, 'new_vault_access_token_hash', 32);

        $vaultsRepo = new VaultsRepository($db);
        if (!method_exists($vaultsRepo, 'rotateAccessTokenHash')) {
            // Defensive guard — controllers should error consistently
            // even if the repo method hasn't landed yet on this branch.
            throw new VaultInvalidRequestError('access-secret rotation not supported on this relay');
        }
        $now = time();
        $vaultsRepo->rotateAccessTokenHash($vaultId, $newHash, $now);

        $grants = new VaultDeviceGrantsRepository($db);
        $triggered = $body['triggered_by_revoke_grant_id'] ?? null;
        $grants->recordRotation(
            $vaultId, $caller, $now,
            $triggered !== null ? (string)$triggered : null
        );

        Router::json([
            'ok' => true,
            'data' => [
                'vault_id'   => self::dashedVaultId($vaultId),
                'rotated_at' => self::ts($now),
            ],
        ], 200);
    }
}
