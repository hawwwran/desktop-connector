<?php

final class TransferState
{
    public const INITIALIZED = 'initialized';
    public const UPLOADING = 'uploading';
    public const UPLOADED = 'uploaded';
    public const DELIVERING = 'delivering';
    public const DELIVERED = 'delivered';
    public const EXPIRED = 'expired';

    private const ALLOWED = [
        self::INITIALIZED,
        self::UPLOADING,
        self::UPLOADED,
        self::DELIVERING,
        self::DELIVERED,
        self::EXPIRED,
    ];

    private string $value;

    private function __construct(string $value)
    {
        if (!in_array($value, self::ALLOWED, true)) {
            throw new InvalidArgumentException('Invalid transfer state: ' . $value);
        }
        $this->value = $value;
    }

    public static function from(string $value): self
    {
        return new self($value);
    }

    public function value(): string
    {
        return $this->value;
    }

    public function is(string $value): bool
    {
        return $this->value === $value;
    }
}
