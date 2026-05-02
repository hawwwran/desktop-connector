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
