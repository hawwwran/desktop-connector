<?php

/**
 * Simple file-based logger with 2-file rotation.
 * When the current log exceeds MAX_SIZE, the old backup is deleted,
 * current becomes backup, and a new file is started.
 * Max disk usage: 2 * MAX_SIZE.
 */
class AppLog
{
    private const MAX_SIZE = 1_000_000; // 1 MB per file
    private const LOG_DIR = __DIR__ . '/../data/logs';
    private const LOG_FILE = 'server.log';

    public static function log(string $tag, string $message): void
    {
        $dir = self::LOG_DIR;
        if (!is_dir($dir)) {
            mkdir($dir, 0700, true);
        }

        $path = $dir . '/' . self::LOG_FILE;
        $backup = $path . '.1';

        // Rotate if current file is too big
        if (file_exists($path) && filesize($path) >= self::MAX_SIZE) {
            if (file_exists($backup)) {
                unlink($backup);
            }
            rename($path, $backup);
        }

        $ts = date('Y-m-d H:i:s');
        $line = "[$ts] [$tag] $message\n";
        file_put_contents($path, $line, FILE_APPEND | LOCK_EX);
    }
}
