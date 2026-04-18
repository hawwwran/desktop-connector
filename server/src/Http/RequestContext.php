<?php

/**
 * Per-request value object built by the Router before dispatching to a
 * controller. Carries everything a handler needs off the HTTP boundary
 * (route params, query string, parsed JSON / raw body, authenticated
 * device id) so controllers stop reaching into superglobals.
 *
 * Body reads are lazy: chunk uploads reach 2 MB and we only want to pull
 * them when a handler actually asks.
 */
class RequestContext
{
    private ?array $jsonBody = null;
    private ?string $rawBodyCache = null;

    public function __construct(
        public readonly string $method,
        public readonly array $params = [],
        public readonly array $query = [],
        public ?string $deviceId = null,
    ) {}

    public function rawBody(): string
    {
        if ($this->rawBodyCache === null) {
            $this->rawBodyCache = file_get_contents('php://input') ?: '';
        }
        return $this->rawBodyCache;
    }

    /**
     * Parsed JSON body, or [] when the body is empty or unparseable.
     * Handlers that require a specific shape should run validators on
     * the returned array.
     */
    public function jsonBody(): array
    {
        if ($this->jsonBody === null) {
            $raw = $this->rawBody();
            if ($raw === '') {
                $this->jsonBody = [];
            } else {
                $decoded = json_decode($raw, true);
                $this->jsonBody = is_array($decoded) ? $decoded : [];
            }
        }
        return $this->jsonBody;
    }
}
