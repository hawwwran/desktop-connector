<?php

/**
 * Resolves on-disk paths for vault chunk blobs (D13 layout):
 *   <root>/vaults/<vault_id>/<chunk_id_prefix>/<chunk_id>
 *
 * Production uses `__DIR__/../storage/`; tests override the root via
 * setRoot() in setUp() and reset to null in tearDown() so they don't
 * pollute the real storage directory. Mirrors the
 * `tempnam(sys_get_temp_dir(), …)` pattern the SQLite tests use.
 */
class VaultStorage
{
    private static ?string $rootOverride = null;

    /** Override the storage root. Pass null to restore the production default. */
    public static function setRoot(?string $path): void
    {
        self::$rootOverride = $path;
    }

    /** Absolute path of the storage root directory. Auto-created on first chunk write. */
    public static function root(): string
    {
        if (self::$rootOverride !== null) {
            return self::$rootOverride;
        }
        return __DIR__ . '/../storage';
    }

    /** Absolute path for a specific chunk's on-disk blob. */
    public static function chunkAbsolutePath(string $vaultId, string $chunkId): string
    {
        return self::root() . '/' . VaultChunksRepository::storagePath($vaultId, $chunkId);
    }

    /**
     * Ensure the parent directory of an absolute path exists. Used before
     * writing a chunk file. Idempotent: returns silently if the directory
     * already exists.
     *
     * Review §1.L3 — directories are created 0700 so only the
     * web-server user can list / read them. This is the right default
     * for mod_php / a single-pool PHP-FPM deployment (one uid serves
     * every request). On a shared-host PHP-FPM with **multiple pools
     * sharing the same documentroot**, each pool runs as its own uid,
     * and 0700 makes chunks written by pool A unreachable from pool B.
     * That layout is rare for first-party Desktop Connector relays
     * (each operator runs their own pool) but if you do share pools,
     * deploy the storage tree on a path that's pre-created with the
     * right group permissions before the relay tries to write, or
     * adjust the umask on the FPM pool to widen the bits.
     */
    public static function ensureDir(string $absolutePath): void
    {
        $dir = dirname($absolutePath);
        if (!is_dir($dir) && !mkdir($dir, 0700, true) && !is_dir($dir)) {
            throw new VaultStorageUnavailableError("Failed to create storage directory: {$dir}");
        }
    }
}
