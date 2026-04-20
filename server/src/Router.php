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

            throw new NotFoundError();
        } catch (ApiError $e) {
            // 5xx and unexpected 4xx are worth a warning; routine 404/401/403
            // stay at info. NotFound routes come from typos or probes — info.
            $level = $e->status >= 500 ? 'error' : 'warning';
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
