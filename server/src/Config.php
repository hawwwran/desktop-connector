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
        // Operator kill-switch for the streaming relay (see
        // docs/plans/streaming-improvement.md). When false, /api/health
        // drops `stream_v1` from capabilities and /api/transfers/init
        // always returns negotiated_mode=classic so the entire fleet
        // falls back to store-then-forward without a code change.
        'streamingEnabled' => true,
        // Review §1.L2: ``migrationStart`` / ``migrationCommit`` persist
        // the target relay URL into the vaults table and re-expose it
        // through GET /header to every paired device. The default
        // policy rejects loopback, private (RFC 1918), and link-local
        // hosts so an admin can't redirect the fleet at an internal
        // service. Operators running test rigs that legitimately need
        // local URLs (a paired desktop hitting ``http://127.0.0.1:4441``
        // during development) flip this to true. The toggle is *not*
        // wired through any UI on purpose — it's a deliberate
        // deployment-time choice.
        'migrationAllowPrivateUrls' => false,
        // Per-vault ciphertext cap stamped onto every freshly-created
        // vault row. Matches the pre-2026-05-19 schema default of 1 GB
        // (``migrations/002_vault.sql`` line 31) when missing. Operators
        // running tight-disk hosts can lower; B5-style live tests can
        // also override to a few MiB to exercise the 507 / eviction
        // paths without filling real storage. Existing vault rows are
        // **not** retroactively resized — the key only affects new
        // vaults. Wired through ``VaultsRepository::create``.
        'vaultQuotaBytes' => 1073741824,
        // Floor on per-(device, vault) vault auth attempts per minute.
        // Hardcoded design (pre-2026-05-19) capped it at 10 / minute
        // to throttle a compromised paired device; the B5 live test
        // surfaced that legitimate sync workloads bill an auth attempt
        // per chunk PUT and easily exceed 10 in a single cycle. The
        // value is now configurable upward — operators on dedicated
        // hosts that prefer raising the cap can; the floor of 10 is
        // enforced in ``vaultAuthLimit()`` so a typo can never
        // *weaken* the throttle below the original design.
        'vaultAuthLimit' => 10,
    ];

    /** Smallest accepted value for ``vaultAuthLimit``. Matches the
     *  original hardcoded ``VaultAuthService::AUTH_LIMIT``. Refusing
     *  values below this preserves the §1.H1 floor even if the
     *  on-disk config carries a typo. */
    public const VAULT_AUTH_LIMIT_FLOOR = 10;

    /** @var array<string,mixed>|null — memoised per-request to avoid
     *  repeated file reads in hot paths. Reset via ::flush() if needed. */
    private static ?array $cached = null;

    /** Once-per-request flag so the clamp warning fires at most once
     *  per request even if ``vaultAuthLimit()`` is called many times
     *  (every chunk PUT). Reset by ::flush() so tests can re-arm it. */
    private static bool $vaultAuthLimitClampLogged = false;

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

    /** Whether the streaming-relay mode is exposed to clients. Tolerant
     *  of non-bool values in the JSON — accepts string "false" / "0" so
     *  an operator editing by hand doesn't have to know PHP-truthiness
     *  rules. Default true (set in self::DEFAULTS). */
    public static function streamingEnabled(): bool
    {
        $raw = self::get('streamingEnabled');
        if (is_bool($raw)) {
            return $raw;
        }
        if (is_string($raw)) {
            $v = strtolower(trim($raw));
            return !in_array($v, ['0', 'false', 'no', 'off', ''], true);
        }
        if (is_int($raw)) {
            return $raw !== 0;
        }
        return (bool)self::DEFAULTS['streamingEnabled'];
    }

    /** Review §1.L2: whether ``migrationStart`` / ``migrationCommit``
     *  accept private / loopback / link-local hosts in the target
     *  relay URL. Default false. Tolerant of non-bool values for
     *  hand-edited config (same shape as ``streamingEnabled``). */
    public static function migrationAllowPrivateUrls(): bool
    {
        $raw = self::get('migrationAllowPrivateUrls');
        if (is_bool($raw)) {
            return $raw;
        }
        if (is_string($raw)) {
            $v = strtolower(trim($raw));
            return in_array($v, ['1', 'true', 'yes', 'on'], true);
        }
        if (is_int($raw)) {
            return $raw !== 0;
        }
        return (bool)self::DEFAULTS['migrationAllowPrivateUrls'];
    }

    /** Per-vault ciphertext cap stamped on freshly-created vaults.
     *  Falls back to the documented 1 GB default when the JSON is
     *  missing the key or carries a non-positive value (an operator
     *  who typed ``0`` would otherwise mint un-fillable vaults). */
    public static function vaultQuotaBytes(): int
    {
        $raw = self::get('vaultQuotaBytes');
        $value = is_numeric($raw) ? (int)$raw : 0;
        if ($value <= 0) {
            return (int)self::DEFAULTS['vaultQuotaBytes'];
        }
        return $value;
    }

    /** Floor-protected per-(device, vault) auth attempts/min. Returns
     *  ``max(configured, VAULT_AUTH_LIMIT_FLOOR)`` so an operator can
     *  raise the cap on dedicated hosts (B5 SO-2: legitimate sync
     *  workloads bill an auth attempt per chunk PUT and exceed 10/min)
     *  but a config typo can never lower it below the original §1.H1
     *  design. Emits ``config.vaultAuthLimit.clamped`` to AppLog at
     *  warning level the first time a sub-floor configured value is
     *  observed within a request — silent clamping would let an
     *  operator think they tightened the throttle while it actually
     *  stayed at 10. */
    public static function vaultAuthLimit(): int
    {
        $raw = self::get('vaultAuthLimit');
        $value = is_numeric($raw) ? (int)$raw : 0;
        if ($value > 0 && $value < self::VAULT_AUTH_LIMIT_FLOOR && !self::$vaultAuthLimitClampLogged) {
            self::$vaultAuthLimitClampLogged = true;
            if (class_exists('AppLog')) {
                AppLog::log(
                    'config.vaultAuthLimit.clamped',
                    sprintf(
                        'configured=%d floor=%d effective=%d',
                        $value,
                        self::VAULT_AUTH_LIMIT_FLOOR,
                        self::VAULT_AUTH_LIMIT_FLOOR,
                    ),
                    'warning',
                );
            }
        }
        return max(self::VAULT_AUTH_LIMIT_FLOOR, $value);
    }

    public static function all(): array
    {
        return array_merge(self::DEFAULTS, self::load());
    }

    public static function flush(): void
    {
        self::$cached = null;
        self::$vaultAuthLimitClampLogged = false;
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
