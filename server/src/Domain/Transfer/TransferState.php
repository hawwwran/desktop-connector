<?php

/**
 * Canonical transfer lifecycle states.
 *
 * Keep this file as the source of truth for lifecycle semantics so API,
 * repositories, cleanup jobs, and notification flows reason about the same
 * invariants.
 */
final class TransferState
{
    /**
     * Sender successfully created transfer metadata, but no chunk persistence is
     * guaranteed yet.
     *
     * Invariants:
     * - transfer row exists.
     * - complete = 0.
     * - downloaded = 0.
     */
    public const INITIALIZED = 'initialized';

    /**
     * At least one upload step is in progress and the transfer is not complete.
     *
     * Invariants:
     * - complete = 0.
     * - downloaded = 0.
     * - transfer can still accept chunks.
     */
    public const UPLOADING = 'uploading';

    /**
     * Sender upload is complete and all chunks are present server-side, but
     * recipient delivery has not begun.
     *
     * Invariants:
     * - complete = 1.
     * - downloaded = 0.
     * - chunks_downloaded = 0.
     */
    public const UPLOADED = 'uploaded';

    /**
     * Recipient has started downloading chunks, but final ACK is not yet
     * recorded.
     *
     * Invariants:
     * - complete = 1.
     * - downloaded = 0.
     * - chunks_downloaded > 0 and < chunk_count.
     */
    public const DELIVERING = 'delivering';

    /**
     * Recipient finalized transfer consumption and server persisted final ACK.
     *
     * Invariants:
     * - complete = 1.
     * - downloaded = 1.
     * - delivered_at > 0.
     */
    public const DELIVERED = 'delivered';

    /**
     * Conceptual terminal state used by cleanup/retention logic when transfer
     * data is no longer valid for upload or delivery.
     *
     * Invariants:
     * - retention window elapsed OR transfer invalidated by cleanup policy.
     * - backing chunks may already be deleted.
     */
    public const EXPIRED = 'expired';
}
