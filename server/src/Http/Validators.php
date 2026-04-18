<?php

/**
 * Small input-validation helpers. All throw ValidationError on miss, so
 * callers can write linear code without early-return boilerplate.
 *
 * Intentionally small — not a DSL, not a framework. If a rule needs more
 * than a single function, it belongs in the service that uses it.
 */
class Validators
{
    // Matches the old TransferService::TRANSFER_ID_PATTERN — alphanumeric +
    // hyphen, capped at 64 chars. Keeps "../" and friends out of
    // server/storage/{transfer_id}/... paths for every endpoint that
    // accepts {transfer_id}, not just the ones that call TransferService.
    private const SAFE_TRANSFER_ID = '/^[a-zA-Z0-9-]{1,64}$/';

    public static function requireNonEmptyString(array $body, string $field): string
    {
        if (empty($body[$field]) || !is_string($body[$field])) {
            throw new ValidationError("Missing $field");
        }
        return $body[$field];
    }

    public static function requireInt(array $body, string $field): int
    {
        if (!isset($body[$field]) || !is_numeric($body[$field])) {
            throw new ValidationError("Missing $field");
        }
        return (int)$body[$field];
    }

    /**
     * Accepts the key being present with `null` as a valid value (used by
     * fcm-token, where explicit null clears the stored token).
     */
    public static function requireNullableString(array $body, string $field): ?string
    {
        if (!array_key_exists($field, $body)) {
            throw new ValidationError("Missing $field");
        }
        $value = $body[$field];
        if ($value === null) {
            return null;
        }
        if (!is_string($value)) {
            throw new ValidationError("Invalid $field");
        }
        return $value;
    }

    public static function requireIntParam(array $params, string $field, int $min = 1): int
    {
        $raw = $params[$field] ?? null;
        if ($raw === null || !is_numeric($raw)) {
            throw new ValidationError("Invalid $field");
        }
        $value = (int)$raw;
        if ($value < $min) {
            throw new ValidationError("Invalid $field");
        }
        return $value;
    }

    /**
     * Path-safe transfer id. Rejects anything that could escape the
     * per-transfer storage directory.
     */
    public static function requireSafeTransferId(array $params, string $field = 'transfer_id'): string
    {
        $value = $params[$field] ?? null;
        if (!is_string($value) || !preg_match(self::SAFE_TRANSFER_ID, $value)) {
            throw new ValidationError('Invalid transfer_id format');
        }
        return $value;
    }
}
