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
        Router::json(['error' => $e->getMessage()] + $e->extra, $e->status);
    }
}
