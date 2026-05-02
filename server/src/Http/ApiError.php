<?php

/**
 * Base for HTTP-mappable errors thrown out of controllers / services.
 * The Router catches ApiError at the top of dispatch() and hands it to
 * ErrorResponder for serialization.
 *
 * `extra` merges into the JSON body alongside `error`; `headers` sets
 * response headers (used by RateLimitError to emit `Retry-After`).
 */
class ApiError extends RuntimeException
{
    public function __construct(
        public readonly int $status,
        string $message,
        public readonly array $extra = [],
        public readonly array $headers = [],
        /**
         * vault_v1 error code, e.g. "vault_auth_failed". When non-null
         * ErrorResponder emits the T0 §"Error codes" envelope:
         *   {"ok": false, "error": {"code": ..., "message": ..., "details": ...}}
         * Legacy non-vault errors leave this null and stay on the
         * existing {"error": "<message>", ...$extra} shape.
         *
         * Named `errorCode` rather than `code` to avoid colliding with
         * Exception::$code (which carries the integer libc-style code).
         */
        public readonly ?string $errorCode = null,
        public readonly array $details = [],
    ) {
        parent::__construct($message);
    }
}

class ValidationError extends ApiError
{
    public function __construct(string $message)
    {
        parent::__construct(400, $message);
    }
}

class UnauthorizedError extends ApiError
{
    public function __construct(string $message = 'Missing authentication')
    {
        parent::__construct(401, $message);
    }
}

class ForbiddenError extends ApiError
{
    public function __construct(string $message)
    {
        parent::__construct(403, $message);
    }
}

class NotFoundError extends ApiError
{
    public function __construct(string $message = 'Not found')
    {
        parent::__construct(404, $message);
    }
}

class ConflictError extends ApiError
{
    public function __construct(string $message)
    {
        parent::__construct(409, $message);
    }
}

/**
 * 429 with the exact on-wire shape that `DeviceController::ping` used
 * to emit by hand: Retry-After header + retry_after body field.
 */
class RateLimitError extends ApiError
{
    public function __construct(string $message, int $retryAfter)
    {
        parent::__construct(
            429,
            $message,
            ['retry_after' => $retryAfter],
            ['Retry-After' => (string)$retryAfter],
        );
    }
}

class StorageLimitError extends ApiError
{
    public function __construct(string $message)
    {
        parent::__construct(507, $message);
    }
}

/**
 * 413 — this specific request is larger than the server's configured
 * cap, independent of current usage. Distinct from StorageLimitError
 * (507) which is transient: waiting for existing queued transfers to
 * drain makes room. 413 is terminal; the client should surface
 * "exceeds server quota" and not retry.
 */
class PayloadTooLargeError extends ApiError
{
    public function __construct(string $message)
    {
        parent::__construct(413, $message);
    }
}

/**
 * 425 Too Early — streaming download only. The transfer exists and is
 * not aborted, but the chunk hasn't been stored yet. The recipient
 * should retry after `retry_after_ms`. Distinct from 404 (transfer
 * unknown / wiped, terminal) so the recipient can poll politely
 * instead of treating a transient upstream gap as a fatal error.
 *
 * Emits both a `Retry-After` HTTP header (seconds, per RFC 7231) and
 * a millisecond-precision `retry_after_ms` body field — recipients on
 * a hot pipeline want sub-second pacing, but standard HTTP caches /
 * gateways only understand the integer-second header.
 */
class TooEarlyError extends ApiError
{
    public function __construct(string $message, int $retryAfterMs = 1000)
    {
        $retrySec = max(1, (int)ceil($retryAfterMs / 1000));
        parent::__construct(
            425,
            $message,
            ['retry_after_ms' => $retryAfterMs],
            ['Retry-After' => (string)$retrySec],
        );
    }
}

/**
 * 410 Gone — the transfer has been aborted (by either party) or has
 * been fully wiped after streaming completion. Terminal from the
 * caller's perspective: no retry helps. Distinct from 404 because the
 * transfer_id WAS valid at some point; the caller may want to reflect
 * that in the UI ("Aborted" vs "Not found").
 */
class AbortedError extends ApiError
{
    public function __construct(string $message = 'Transfer has been aborted', ?string $reason = null)
    {
        $extra = $reason !== null ? ['abort_reason' => $reason] : [];
        parent::__construct(410, $message, $extra);
    }
}
