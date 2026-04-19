<?php

/**
 * Internal transfer lifecycle states.
 * These are domain-level states; they are not protocol API values.
 */
class TransferState
{
    public const INITIALIZED = 'initialized';
    public const UPLOADING = 'uploading';
    public const UPLOADED = 'uploaded';
    public const DELIVERING = 'delivering';
    public const DELIVERED = 'delivered';
    public const EXPIRED = 'expired';

    public static function all(): array
    {
        return [
            self::INITIALIZED,
            self::UPLOADING,
            self::UPLOADED,
            self::DELIVERING,
            self::DELIVERED,
            self::EXPIRED,
        ];
    }
}
