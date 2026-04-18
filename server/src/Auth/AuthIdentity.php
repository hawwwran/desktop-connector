<?php

/**
 * Result of a successful authentication. Kept separate from RequestContext
 * so optional-auth callers (e.g. health endpoint) have a clear type for
 * "authenticated vs anonymous" without inspecting context state.
 */
class AuthIdentity
{
    public function __construct(public readonly string $deviceId) {}
}
