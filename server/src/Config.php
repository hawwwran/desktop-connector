<?php

/**
 * Server-side config loaded from server/data/config.json.
 *
 * Auto-creates the file with default values on first access so operators
 * who just drop the server onto shared hosting don't need to ship a
 * pre-written config. Values can be tuned by editing the JSON directly;
 * changes take effect on the next request (no restart needed, the file
 * is re-read per Config::get() call within a request).
 *
 * Keep the surface small: each setting is a top-level key in the JSON.
 * Defaults live in self::DEFAULTS so a corrupt or partial file still
 * yields a working server with documented fall-backs.
 */
class Config
{
    /** Defaults applied when the key is missing from the on-disk JSON. */
    private const DEFAULTS = [
        'storageQuotaMB' => 500,
    ];

    /** @var array<string,mixed>|null — memoised per-request to avoid
     *  repeated file reads in hot paths. Reset via ::flush() if needed. */
    private static ?array $cached = null;

    public static function get(string $key): mixed
    {
        $data = self::load();
        return $data[$key] ?? self::DEFAULTS[$key] ?? null;
    }

    /** Storage limit in bytes. Convenience wrapper used by TransferService /
     *  dashboard so neither re-does the MB→bytes arithmetic. */
    public static function storageQuotaBytes(): int
    {
        $mb = (int)self::get('storageQuotaMB');
        return $mb * 1024 * 1024;
    }

    public static function all(): array
    {
        return array_merge(self::DEFAULTS, self::load());
    }

    public static function flush(): void
    {
        self::$cached = null;
    }

    private static function load(): array
    {
        if (self::$cached !== null) {
            return self::$cached;
        }
        $path = self::path();
        if (!is_file($path)) {
            self::writeDefaults($path);
        }
        $raw = @file_get_contents($path);
        $data = is_string($raw) ? @json_decode($raw, true) : null;
        if (!is_array($data)) {
            // Corrupt file — fall back to defaults without touching disk so
            // a human can see and fix the bad JSON.
            $data = [];
        }
        self::$cached = $data;
        return $data;
    }

    private static function writeDefaults(string $path): void
    {
        $dir = dirname($path);
        if (!is_dir($dir)) {
            @mkdir($dir, 0700, true);
        }
        @file_put_contents(
            $path,
            json_encode(self::DEFAULTS, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n",
        );
    }

    private static function path(): string
    {
        // data/ lives alongside the SQLite DB; already .htaccess-protected
        // from HTTP access so operators can't accidentally leak future
        // sensitive config values.
        return __DIR__ . '/../data/config.json';
    }
}
