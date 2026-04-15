<?php

class Router
{
    private array $routes = [];

    public function add(string $method, string $pattern, callable $handler): void
    {
        $this->routes[] = [
            'method' => strtoupper($method),
            'pattern' => $pattern,
            'handler' => $handler,
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

    public function dispatch(string $method, string $uri): void
    {
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
            if ($params !== false) {
                ($route['handler'])($params);
                return;
            }
        }

        self::json(['error' => 'Not found'], 404);
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

    public static function getJsonBody(): ?array
    {
        $raw = file_get_contents('php://input');
        if (empty($raw)) {
            return null;
        }
        return json_decode($raw, true);
    }

    public static function getRawBody(): string
    {
        return file_get_contents('php://input');
    }

    /**
     * Authenticate request via X-Device-ID + Authorization: Bearer <token>.
     * Returns device_id on success, sends 401 and returns null on failure.
     */
    public static function authenticate(Database $db): ?string
    {
        $deviceId = $_SERVER['HTTP_X_DEVICE_ID'] ?? null;
        $authHeader = $_SERVER['HTTP_AUTHORIZATION'] ?? '';

        if (!$deviceId || !str_starts_with($authHeader, 'Bearer ')) {
            self::json(['error' => 'Missing authentication'], 401);
            return null;
        }

        $token = substr($authHeader, 7);
        $device = $db->querySingle(
            'SELECT device_id FROM devices WHERE device_id = :id AND auth_token = :token',
            [':id' => $deviceId, ':token' => $token]
        );

        if (!$device) {
            self::json(['error' => 'Invalid credentials'], 401);
            return null;
        }

        // Update last_seen
        $db->execute(
            'UPDATE devices SET last_seen_at = :now WHERE device_id = :id',
            [':now' => time(), ':id' => $deviceId]
        );

        return $deviceId;
    }
}
