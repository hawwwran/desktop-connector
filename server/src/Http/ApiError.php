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
