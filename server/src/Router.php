<?php

class Router
{
    private array $routes = [];

    public function __construct(private Database $db) {}

    public function add(string $method, string $pattern, callable $handler, bool $requiresAuth = false): void
    {
        $this->routes[] = [
            'method' => strtoupper($method),
            'pattern' => $pattern,
            'handler' => $handler,
            'requiresAuth' => $requiresAuth,
        ];
    }

    public function get(string $pattern, callable $handler): void
    {
        $this->add('GET', $pattern, $handler);
    }

    public function post(string $pattern, callable $handler): void
    {
        $this->add('POST', $pattern, $handler);
    }

    public function authGet(string $pattern, callable $handler): void
    {
        $this->add('GET', $pattern, $handler, requiresAuth: true);
    }

    public function authPost(string $pattern, callable $handler): void
    {
        $this->add('POST', $pattern, $handler, requiresAuth: true);
    }

    public function authDelete(string $pattern, callable $handler): void
    {
        $this->add('DELETE', $pattern, $handler, requiresAuth: true);
    }

    public function authPut(string $pattern, callable $handler): void
    {
        $this->add('PUT', $pattern, $handler, requiresAuth: true);
    }

    public function authHead(string $pattern, callable $handler): void
    {
        $this->add('HEAD', $pattern, $handler, requiresAuth: true);
    }

    /**
     * Vault-routing helpers. Vault endpoints emit the T0 vault_v1 error
     * envelope and need a custom auth path (VaultAuthService composes
     * device + vault auth and translates failures to vault_auth_failed
     * with a kind discriminator). The Router's pre-handler
     * AuthService::requireAuth would emit the legacy `{"error": "..."}`
     * shape on device-auth failure, so we skip it here and let the
     * controller call VaultAuthService::requireVaultAuth itself.
     *
     * Effectively these are "auth-required at the controller layer"
     * registrations — the auth still happens, just one level deeper.
     */
    public function vaultPost(string $pattern, callable $handler): void
    {
        $this->add('POST', $pattern, $handler, requiresAuth: false);
    }

    public function vaultGet(string $pattern, callable $handler): void
    {
        $this->add('GET', $pattern, $handler, requiresAuth: false);
    }

    public function vaultPut(string $pattern, callable $handler): void
    {
        $this->add('PUT', $pattern, $handler, requiresAuth: false);
    }

    public function vaultHead(string $pattern, callable $handler): void
    {
        $this->add('HEAD', $pattern, $handler, requiresAuth: false);
    }

    public function vaultDelete(string $pattern, callable $handler): void
    {
        $this->add('DELETE', $pattern, $handler, requiresAuth: false);
    }

    public function dispatch(string $method, string $uri): void
    {
        try {
            $uri = parse_url($uri, PHP_URL_PATH);

            // Strip base path for subdirectory deployments
            $scriptName = $_SERVER['SCRIPT_NAME'] ?? '';
            $basePath = rtrim(dirname(dirname($scriptName)), '/');
            if ($basePath && str_starts_with($uri, $basePath)) {
                $uri = substr($uri, strlen($basePath));
            }

            $uri = rtrim($uri, '/');
            if ($uri === '') {
                $uri = '/';
            }

            foreach ($this->routes as $route) {
                if ($route['method'] !== strtoupper($method)) {
                    continue;
                }
                $params = $this->match($route['pattern'], $uri);
                if ($params === false) {
                    continue;
                }

                $ctx = new RequestContext(
                    method: strtoupper($method),
                    params: $params,
                    query: $_GET,
                );
                if ($route['requiresAuth']) {
                    $identity = AuthService::requireAuth($this->db);
                    $ctx->deviceId = $identity->deviceId;
                }
                ($route['handler'])($ctx);
                return;
            }

            // F-S20: vault-namespace 404s emit the vault_v1 envelope so
            // feature-detecting clients see a code they recognise.
            if (str_starts_with($uri, '/api/vaults')) {
                throw new VaultApiError(
                    status: 404,
                    errorCode: 'vault_not_found',
                    message: "Not found: {$method} {$uri}",
                );
            }
            throw new NotFoundError();
        } catch (ApiError $e) {
            // 5xx: error. 425: debug — streaming pipelines emit one per
            // chunk-poll before the sender catches up; logging each at
            // warning would spam the log. Other 4xx: warning.
            $level = match (true) {
                $e->status >= 500 => 'error',
                $e->status === 425 => 'debug',
                default => 'warning',
            };
            AppLog::log('Api', sprintf(
                'apierror.caught status=%d method=%s uri=%s reason=%s',
                $e->status,
                $_SERVER['REQUEST_METHOD'] ?? '-',
                $_SERVER['REQUEST_URI'] ?? '-',
                $e->getMessage()
            ), $level);
            ErrorResponder::send($e);
        }
    }

    private function match(string $pattern, string $uri): array|false
    {
        // Convert {param} to named regex groups
        $regex = preg_replace('/\{(\w+)\}/', '(?P<$1>[^/]+)', $pattern);
        $regex = '#^' . $regex . '$#';

        if (preg_match($regex, $uri, $matches)) {
            return array_filter($matches, 'is_string', ARRAY_FILTER_USE_KEY);
        }
        return false;
    }

    public static function json(mixed $data, int $status = 200): void
    {
        http_response_code($status);
        header('Content-Type: application/json');
        echo json_encode($data);
    }

    public static function binary(string $data, int $status = 200): void
    {
        http_response_code($status);
        header('Content-Type: application/octet-stream');
        header('Content-Length: ' . strlen($data));
        echo $data;
    }
}
