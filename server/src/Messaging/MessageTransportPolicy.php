<?php

/**
 * Transport policy for command-style messages.
 *
 * Server cannot inspect encrypted semantics, but it can still enforce
 * coarse transport limits (e.g. fasttrack payload size ceiling) and keep
 * transport intent explicit in one place.
 */
class MessageTransportPolicy
{
    private const FASTTRACK_MAX_ENCRYPTED_BYTES = 128 * 1024;

    public static function fasttrackMaxEncryptedBytes(): int
    {
        return self::FASTTRACK_MAX_ENCRYPTED_BYTES;
    }

    public static function isFasttrackPayloadSizeAllowed(int $bytes): bool
    {
        return $bytes > 0 && $bytes <= self::FASTTRACK_MAX_ENCRYPTED_BYTES;
    }
}
