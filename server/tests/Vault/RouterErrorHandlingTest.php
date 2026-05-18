<?php

declare(strict_types=1);

use PHPUnit\Framework\TestCase;

/**
 * Review §1.M4 — the Router's top-level dispatch must catch
 * \Throwable, not just ApiError. Pre-fix an uncaught generic
 * exception (PDO error, type error, runtime crash in a controller)
 * fell through to PHP's default error handler, which can leak
 * stack frames + file paths + SQL fragments via the standard error
 * envelope unless the operator remembered ``display_errors=Off``.
 *
 * Pinned via a focused dispatch test: register a route handler that
 * throws a plain ``\RuntimeException``, capture stdout, and assert
 * the response is the typed ``vault_internal_error`` envelope with
 * no exception details exposed.
 */
final class RouterErrorHandlingTest extends TestCase
{
    private string $dbPath;
    private Database $db;

    protected function setUp(): void
    {
        $this->dbPath = tempnam(sys_get_temp_dir(), 'router_test_') . '.sqlite';
        $this->db = Database::fromPath($this->dbPath);
    }

    protected function tearDown(): void
    {
        @unlink($this->dbPath);
    }

    public function test_uncaught_throwable_emits_typed_envelope(): void
    {
        $router = new Router($this->db);
        $router->get('/api/test/boom', function ($_ctx) {
            throw new \RuntimeException('boom! private details');
        });

        $captured = $this->captureDispatch($router, 'GET', '/api/test/boom');

        self::assertSame(500, $captured['status']);
        $body = json_decode($captured['body'], true);
        self::assertIsArray($body);
        self::assertFalse($body['ok']);
        self::assertSame('vault_internal_error', $body['error']['code']);
        // Crucially: the original exception message is NOT in the body.
        // (Operator can find it in the server log via the
        // ``apierror.uncaught_throwable`` event.)
        self::assertStringNotContainsString('boom! private details', $captured['body']);
    }

    public function test_thrown_apierror_still_uses_typed_serializer(): void
    {
        // Sanity check: the new \Throwable arm doesn't shadow the
        // existing ApiError branch. A controller throwing a typed
        // VaultApiError still flows through ErrorResponder unchanged.
        $router = new Router($this->db);
        $router->get('/api/test/typed', function ($_ctx) {
            throw new VaultApiError(
                status: 418,
                errorCode: 'vault_test_envelope',
                message: 'expected error',
            );
        });

        $captured = $this->captureDispatch($router, 'GET', '/api/test/typed');
        self::assertSame(418, $captured['status']);
        $body = json_decode($captured['body'], true);
        self::assertSame('vault_test_envelope', $body['error']['code']);
    }

    /**
     * Capture HTTP status + JSON body for one dispatch call.
     *
     * @return array{status:int,body:string}
     */
    private function captureDispatch(Router $router, string $method, string $uri): array
    {
        $_SERVER['REQUEST_METHOD'] = $method;
        $_SERVER['REQUEST_URI'] = $uri;
        $_SERVER['SCRIPT_NAME'] = '/public/index.php';
        ob_start();
        $router->dispatch($method, $uri);
        $body = (string) ob_get_clean();
        $status = http_response_code();
        // Reset so the next test starts at 200.
        http_response_code(200);
        return ['status' => (int) $status, 'body' => $body];
    }
}
