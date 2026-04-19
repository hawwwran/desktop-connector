<?php

/**
 * Transition guards for transfer lifecycle mutations.
 *
 * Canonical transfer states inferred from row flags:
 *   - uploading: complete=0, downloaded=0
 *   - pending:   complete=1, downloaded=0
 *   - delivered: downloaded=1
 */
class TransferLifecycle
{
    public static function onChunkStored(array $transfer): void
    {
        $state = self::stateOf($transfer);
        if ($state !== 'uploading') {
            throw new ConflictError(sprintf(
                'Illegal transfer transition: onChunkStored not allowed from %s',
                $state
            ));
        }
    }

    public static function onUploadCompleted(array $before, array $after): void
    {
        $beforeState = self::stateOf($before);
        $afterState = self::stateOf($after);
        if ($beforeState !== 'uploading' || $afterState !== 'uploading') {
            throw new ConflictError(sprintf(
                'Illegal transfer transition: onUploadCompleted requires uploading row (before=%s after=%s)',
                $beforeState,
                $afterState
            ));
        }

        if ((int)$after['chunks_received'] < (int)$after['chunk_count']) {
            throw new ConflictError('Illegal transfer transition: upload is not complete');
        }
    }

    public static function onRecipientProgress(array $transfer, int $newProgress): void
    {
        $state = self::stateOf($transfer);
        if ($state !== 'pending') {
            throw new ConflictError(sprintf(
                'Illegal transfer transition: onRecipientProgress not allowed from %s',
                $state
            ));
        }

        if ($newProgress < 0 || $newProgress >= (int)$transfer['chunk_count']) {
            throw new ValidationError('Invalid download progress');
        }
    }

    public static function onAckReceived(array $transfer): void
    {
        $state = self::stateOf($transfer);
        if ($state !== 'pending') {
            throw new ConflictError(sprintf(
                'Illegal transfer transition: onAckReceived not allowed from %s',
                $state
            ));
        }
    }

    public static function onTransferExpired(?array $transfer): void
    {
        if ($transfer === null) {
            return;
        }

        // Expiry is terminal cleanup from any persisted state.
        self::stateOf($transfer);
    }

    private static function stateOf(array $transfer): string
    {
        if ((int)($transfer['downloaded'] ?? 0) === 1) {
            return 'delivered';
        }
        if ((int)($transfer['complete'] ?? 0) === 1) {
            return 'pending';
        }
        return 'uploading';
    }
}
