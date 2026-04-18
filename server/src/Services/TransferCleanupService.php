<?php

/**
 * Cleanup policy and storage artifact removal for transfers.
 *
 * Exposes two deletion modes:
 *   - deleteTransferFiles: full delete (files + dir + chunk rows + transfer row)
 *     used by run() for expired transfers.
 *   - deleteChunkFilesAndRows: partial delete (files + dir + chunk rows only)
 *     used by ack() which keeps the transfer row so /sent-status can report
 *     delivered status to the sender.
 *
 * The 1-in-20 sampling that triggers run() during /pending stays in the
 * controller — it's a sampling policy tied to HTTP traffic, not a cleanup
 * concern. Services here stay deterministic.
 */
class TransferCleanupService
{
    private const TRANSFER_EXPIRY = 7 * 24 * 3600;   // 7 days
    private const INCOMPLETE_EXPIRY = 24 * 3600;     // 24 hours
    private const PAIRING_REQUEST_EXPIRY = 3600;     // 1 hour

    public static function run(Database $db): void
    {
        $now = time();

        $old = $db->queryAll(
            'SELECT id FROM transfers WHERE created_at < :cutoff',
            [':cutoff' => $now - self::TRANSFER_EXPIRY]
        );
        foreach ($old as $t) {
            self::deleteTransferFiles($db, $t['id']);
        }

        $incomplete = $db->queryAll(
            'SELECT id FROM transfers WHERE complete = 0 AND created_at < :cutoff',
            [':cutoff' => $now - self::INCOMPLETE_EXPIRY]
        );
        foreach ($incomplete as $t) {
            self::deleteTransferFiles($db, $t['id']);
        }

        $db->execute(
            'DELETE FROM pairing_requests WHERE created_at < :cutoff',
            [':cutoff' => $now - self::PAIRING_REQUEST_EXPIRY]
        );
    }

    /** Full delete: chunk files, directory, chunk rows, AND transfer row. */
    public static function deleteTransferFiles(Database $db, string $transferId): void
    {
        self::deleteChunkFilesAndRows($db, $transferId);
        $db->execute('DELETE FROM transfers WHERE id = :tid', [':tid' => $transferId]);
    }

    /** Partial delete: chunk files, directory, chunk rows. Transfer row preserved. */
    public static function deleteChunkFilesAndRows(Database $db, string $transferId): void
    {
        $chunks = $db->queryAll(
            'SELECT blob_path FROM chunks WHERE transfer_id = :tid',
            [':tid' => $transferId]
        );
        foreach ($chunks as $chunk) {
            $path = __DIR__ . '/../../storage/' . $chunk['blob_path'];
            if (file_exists($path)) {
                unlink($path);
            }
        }
        $dir = __DIR__ . '/../../storage/' . $transferId;
        if (is_dir($dir)) {
            @rmdir($dir);
        }
        $db->execute('DELETE FROM chunks WHERE transfer_id = :tid', [':tid' => $transferId]);
    }
}
