<?php

declare(strict_types=1);

/**
 * Test helper: temporarily override values in ``server/data/config.json``.
 *
 * Tests that exercise ``Config::*()`` typed wrappers (``vaultQuotaBytes``,
 * ``vaultAuthLimit``, ``migrationAllowPrivateUrls``, …) need to swap the
 * on-disk JSON for a single test body and put it back afterwards.
 * Inline ``try/finally`` save/restore was repeated in 3 places before
 * this helper landed (2026-05-19 review of B5 SO-1/SO-2 wiring).
 *
 * Usage:
 *
 *     final class FooTest extends TestCase {
 *         use ConfigOverrideTrait;
 *
 *         public function test_x(): void {
 *             $this->withConfigOverride(
 *                 ['vaultAuthLimit' => 15],
 *                 function () {
 *                     // ... body runs with the override active ...
 *                 },
 *             );
 *         }
 *     }
 *
 * Restore happens in a ``finally`` block, so the original config is
 * recovered even if the body throws. ``Config::flush()`` runs in both
 * directions to clear the per-request cache.
 *
 * **NOT thread-safe.** The PHPUnit suite runs sequentially today; if
 * ``--process-isolation`` or ``paratest`` ever lands, two tests that
 * mutate ``server/data/config.json`` simultaneously will race. Move
 * to a per-process config path before that change.
 */
trait ConfigOverrideTrait
{
    private function configPath(): string
    {
        return __DIR__ . '/../../data/config.json';
    }

    /**
     * Run $body with $overrides written into config.json. Restore
     * the file (or remove if absent before) on the way out. Flush
     * Config's static cache in both directions.
     *
     * @param array<string,mixed> $overrides
     * @param callable():void $body
     */
    protected function withConfigOverride(array $overrides, callable $body): void
    {
        $path = $this->configPath();
        $backup = is_file($path) ? file_get_contents($path) : null;
        try {
            file_put_contents($path, json_encode($overrides));
            Config::flush();
            $body();
        } finally {
            if ($backup === null) {
                @unlink($path);
            } else {
                file_put_contents($path, $backup);
            }
            Config::flush();
        }
    }
}
