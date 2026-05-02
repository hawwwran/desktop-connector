<?php

/**
 * Single place that turns an ApiError into an HTTP response. The Router
 * calls this from the catch() at the top of dispatch().
 */
class ErrorResponder
{
    public static function send(ApiError $e): void
    {
        foreach ($e->headers as $name => $value) {
            header($name . ': ' . $value);
        }
        if ($e->errorCode !== null) {
            // vault_v1 envelope (T0 §"Error codes"). The whole vault
            // surface uses this shape; legacy transfer/fasttrack/pairing
            // endpoints stay on the older {"error": "..."} form below.
            Router::json([
                'ok' => false,
                'error' => [
                    'code' => $e->errorCode,
                    'message' => $e->getMessage(),
                    'details' => (object)$e->details,
                ],
            ], $e->status);
            return;
        }
        Router::json(['error' => $e->getMessage()] + $e->extra, $e->status);
    }
}
