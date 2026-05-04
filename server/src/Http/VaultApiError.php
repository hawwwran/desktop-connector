<?php

/**
 * Vault-specific subclasses of ApiError. These set the `code` field so
 * ErrorResponder emits the T0 §"Error codes" envelope shape rather than
 * the legacy `{"error": "..."}` form. One subclass per stable error code
 * keeps controllers terse: `throw new VaultAuthFailedError('vault')`.
 *
 * Adding new vault errors here is fine; renaming or repurposing existing
 * codes is forbidden (T0 §"Error codes" — codes are stable forever).
 */
class VaultApiError extends ApiError
{
    public function __construct(
        int $status,
        string $errorCode,
        string $message,
        array $details = [],
        array $headers = [],
    ) {
        parent::__construct(
            status: $status,
            message: $message,
            extra: [],
            headers: $headers,
            errorCode: $errorCode,
            details: $details,
        );
    }
}

/**
 * 401 vault_auth_failed. `kind` is `"device"` when device auth (X-Device-ID
 * + Authorization) was missing / wrong, `"vault"` when X-Vault-Authorization
 * was missing / wrong. Distinguishes the two so the client can prompt for
 * the right credential type (re-pair vs re-enter vault unlock).
 */
class VaultAuthFailedError extends VaultApiError
{
    public function __construct(string $kind = 'vault')
    {
        if ($kind !== 'device' && $kind !== 'vault') {
            throw new InvalidArgumentException("kind must be 'device' or 'vault', got: {$kind}");
        }
        parent::__construct(
            status: 401,
            errorCode: 'vault_auth_failed',
            message: 'Missing or invalid authentication',
            details: ['kind' => $kind],
        );
    }
}

/**
 * 404 vault_not_found. Distinct from 401 so a caller with valid device
 * auth but a typo in the vault_id learns the difference.
 */
class VaultNotFoundError extends VaultApiError
{
    public function __construct(string $vaultId)
    {
        parent::__construct(
            status: 404,
            errorCode: 'vault_not_found',
            message: "Vault not found: {$vaultId}",
            details: ['vault_id' => $vaultId],
        );
    }
}

/**
 * 400 vault_invalid_request. Generic catch-all for malformed request
 * payloads. `field` names the offending parameter when applicable.
 */
class VaultInvalidRequestError extends VaultApiError
{
    public function __construct(string $reason, ?string $field = null)
    {
        $details = ['reason' => $reason];
        if ($field !== null) {
            $details['field'] = $field;
        }
        parent::__construct(
            status: 400,
            errorCode: 'vault_invalid_request',
            message: $reason,
            details: $details,
        );
    }
}

/**
 * 409 vault_already_exists. POST /api/vaults collided with an existing
 * vault_id. Per T0: vault_id is 60 random bits so this is exceptional;
 * the surface is here to keep the wire shape complete.
 */
class VaultAlreadyExistsError extends VaultApiError
{
    public function __construct(string $vaultId)
    {
        parent::__construct(
            status: 409,
            errorCode: 'vault_already_exists',
            message: "Vault already exists: {$vaultId}",
            details: ['vault_id' => $vaultId],
        );
    }
}

/**
 * 409 vault_manifest_conflict. CAS mismatch on PUT /header or
 * PUT /manifest. For manifest publishes the §A1 conflict shape returns
 * the current manifest ciphertext alongside the revision/hash so the
 * client can run §D4 merge in one round-trip; pass the full payload
 * via $details.
 */
class VaultManifestConflictError extends VaultApiError
{
    public function __construct(array $details, string $message = 'The vault manifest changed on the server.')
    {
        parent::__construct(
            status: 409,
            errorCode: 'vault_manifest_conflict',
            message: $message,
            details: $details,
        );
    }
}

/**
 * 404 vault_chunk_missing. Referenced chunk not present on the relay.
 * Auto-retry per T0 retry table; clients exhaust their budget then
 * surface as permanent.
 */
class VaultChunkMissingError extends VaultApiError
{
    public function __construct(string $chunkId)
    {
        parent::__construct(
            status: 404,
            errorCode: 'vault_chunk_missing',
            message: "Chunk not found: {$chunkId}",
            details: ['chunk_id' => $chunkId],
        );
    }
}

/**
 * 422 vault_chunk_size_mismatch. Same chunk_id stored at a different
 * ciphertext_size — a strong signal of either a client bug or a
 * tampered upload attempt. Permanent, never retried.
 */
class VaultChunkSizeMismatchError extends VaultApiError
{
    public function __construct(string $chunkId, int $expectedSize, int $actualSize)
    {
        parent::__construct(
            status: 422,
            errorCode: 'vault_chunk_size_mismatch',
            message: "chunk_id {$chunkId} ciphertext_size mismatch",
            details: [
                'chunk_id'      => $chunkId,
                'expected_size' => $expectedSize,
                'actual_size'   => $actualSize,
            ],
        );
    }
}

/**
 * 422 vault_chunk_tampered. Same chunk_id + same size but a different
 * hash. The Poly1305 tag is the real defense; this is the relay's
 * earliest possible early-warning.
 */
class VaultChunkTamperedError extends VaultApiError
{
    public function __construct(string $chunkId, string $expectedHash, string $actualHash)
    {
        parent::__construct(
            status: 422,
            errorCode: 'vault_chunk_tampered',
            message: "chunk_id {$chunkId} hash mismatch",
            details: [
                'chunk_id'      => $chunkId,
                'expected_hash' => $expectedHash,
                'actual_hash'   => $actualHash,
            ],
        );
    }
}

/**
 * 507 vault_quota_exceeded. The write would push the vault past
 * `quota_ciphertext_bytes`. `eviction_available` drives the client's
 * §D2 eviction-pass decision: true → run pass, retry; false → surface
 * "vault full, sync stopped" terminal banner.
 */
class VaultQuotaExceededError extends VaultApiError
{
    public function __construct(int $usedBytes, int $quotaBytes, bool $evictionAvailable)
    {
        parent::__construct(
            status: 507,
            errorCode: 'vault_quota_exceeded',
            message: 'Vault quota exceeded',
            details: [
                'used_bytes'         => $usedBytes,
                'quota_bytes'        => $quotaBytes,
                'eviction_available' => $evictionAvailable,
            ],
        );
    }
}

/**
 * 409 vault_migration_in_progress. Caller asked for an op that
 * conflicts with the current §H2 migration state — e.g. write to a
 * vault whose `migrated_to` is set, or `commit` before `verify`.
 */
class VaultMigrationInProgressError extends VaultApiError
{
    public function __construct(string $state, ?string $targetRelayUrl = null)
    {
        $details = ['state' => $state];
        if ($targetRelayUrl !== null) {
            $details['target_relay_url'] = $targetRelayUrl;
        }
        parent::__construct(
            status: 409,
            errorCode: 'vault_migration_in_progress',
            message: "Vault migration in progress (state={$state})",
            details: $details,
        );
    }
}

/**
 * 503 vault_storage_unavailable. Relay-side I/O issue — disk error,
 * filesystem unmounted, etc. Auto-retryable.
 */
class VaultStorageUnavailableError extends VaultApiError
{
    public function __construct(string $reason = 'Relay storage unavailable')
    {
        parent::__construct(
            status: 503,
            errorCode: 'vault_storage_unavailable',
            message: $reason,
        );
    }
}

/**
 * 403 vault_access_denied (T13). The caller authenticated successfully,
 * but the device is no longer permitted to act on the vault — the grant
 * has been revoked, the role doesn't allow the requested operation, or
 * the join-request lifecycle says the row isn't in a state the caller
 * can manipulate.
 */
class VaultAccessDeniedError extends VaultApiError
{
    public function __construct(string $reason, ?string $field = null)
    {
        $details = ['reason' => $reason];
        if ($field !== null) {
            $details['field'] = $field;
        }
        parent::__construct(
            status: 403,
            errorCode: 'vault_access_denied',
            message: $reason,
            details: $details,
        );
    }
}

/**
 * 404 vault_join_request_not_found / state-conflict. The join-request id
 * doesn't exist, or the row is in a state the operation can't act on
 * (e.g. claim on already-claimed). Distinct from 403 so a stale QR's
 * code is detectable without ambiguity.
 */
class VaultJoinRequestStateError extends VaultApiError
{
    public function __construct(string $reason, int $status = 404)
    {
        parent::__construct(
            status: $status,
            errorCode: 'vault_join_request_state',
            message: $reason,
            details: ['reason' => $reason],
        );
    }
}
