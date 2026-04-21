<?php

/**
 * Core transfer orchestration: init, upload, pending list, download, ack,
 * per-chunk ack (streaming), abort.
 *
 * Business-level failures throw ApiError subclasses (NotFoundError,
 * ForbiddenError, TooEarlyError, AbortedError, …); the Router catches
 * these and serializes uniformly. Success paths return plain data —
 * downloadChunk returns raw bytes so the controller can emit them via
 * Router::binary.
 *
 * Input-shape and path-safety validation happens at the HTTP boundary
 * (Validators::requireSafeTransferId, requireNonEmptyString, requireInt)
 * before calling these methods; the service assumes its inputs are shaped.
 *
 * Side effects route through sibling services: classic upload completion
 * fires TransferWakeService::wake; streaming first-chunk stored fires
 * TransferWakeService::wakeStreamReady; abort fires wakeAbort on the
 * opposite party. Ack removes chunk storage via
 * TransferCleanupService::deleteChunkFilesAndRows.
 */
// Defensive require — matches DashboardController's pattern so a
// partial upload that forgets index.php still loads Config.
require_once __DIR__ . '/../Config.php';

class TransferService
{
    private const MAX_CHUNK_COUNT = 500;
    /** Must match the clients' CHUNK_SIZE. Used to project a transfer's
     *  eventual on-disk size at init-time so the quota can reserve space
     *  before chunks arrive — without this, N transfers could each look
     *  under-quota at init while their combined payloads blow past it. */
    private const PROJECTED_CHUNK_SIZE = 2 * 1024 * 1024;

    /** Negotiation threshold for "recipient is online right now". Aligns
     *  with the design in docs/plans/streaming-improvement.md §gap 11 —
     *  stricter than the 120s pairing heartbeat so streaming never
     *  commits to a recipient who's already drifting away. */
    private const STREAM_ONLINE_WINDOW_SEC = 15;

    /** Retry hint returned by 425 Too Early on streaming downloads.
     *  The plan allows the recipient to layer its own backoff above the
     *  server hint; this number is just "come back in a second". */
    private const STREAM_TOO_EARLY_RETRY_MS = 1000;

    public static function init(
        Database $db,
        string $senderId,
        string $transferId,
        string $recipientId,
        string $encryptedMeta,
        int $chunkCount,
        ?string $requestedMode = null,
    ): array {
        if ($chunkCount < 1 || $chunkCount > self::MAX_CHUNK_COUNT) {
            throw new ValidationError('Invalid chunk_count');
        }

        $negotiatedMode = self::negotiateMode($db, $recipientId, $requestedMode);

        $transferRepo = new TransferRepository($db);
        $quotaBytes = Config::storageQuotaBytes();

        if ($negotiatedMode === 'classic') {
            // Quota projection (original behaviour, unchanged). Projects
            // the full eventual size of the new transfer PLUS every
            // still-in-flight transfer to this recipient. Without the
            // projection, init can accept a new big transfer because no
            // chunks have uploaded yet (0 bytes counted), and per-chunk
            // uploads have no second quota gate — so the server quietly
            // overshoots the cap.
            $pendingBytes = (new ChunkRepository($db))->sumPendingBytesForRecipient($recipientId);
            $reservedChunks = $transferRepo->sumPendingChunkCountForRecipient($recipientId);
            $reservedProjected = max($pendingBytes, $reservedChunks * self::PROJECTED_CHUNK_SIZE);
            $newProjected = $chunkCount * self::PROJECTED_CHUNK_SIZE;
            if ($newProjected > $quotaBytes) {
                throw new PayloadTooLargeError('Transfer exceeds server quota');
            }
            if ($reservedProjected + $newProjected > $quotaBytes) {
                throw new StorageLimitError('Recipient storage limit exceeded');
            }
        } else {
            // Streaming mode: skip projected reservation. Peak on-disk
            // footprint is 1–few chunks because the recipient drains as
            // the sender uploads. Only reject here for the pathological
            // case where a single chunk can't fit — there's nothing the
            // sender can do about that, so surface it up front as 413
            // instead of letting every chunk upload bounce on 507.
            if ($quotaBytes < self::PROJECTED_CHUNK_SIZE) {
                throw new PayloadTooLargeError('Server quota smaller than one chunk');
            }
        }

        if ($transferRepo->existsById($transferId)) {
            throw new ConflictError('Transfer ID already exists');
        }

        $transferRepo->insertTransfer(
            $transferId,
            $senderId,
            $recipientId,
            $encryptedMeta,
            $chunkCount,
            time(),
            $negotiatedMode,
        );
        AppLog::log('Transfer', sprintf(
            'transfer.init.accepted transfer_id=%s sender=%s recipient=%s chunks=%d mode=%s',
            AppLog::shortId($transferId),
            AppLog::shortId($senderId),
            AppLog::shortId($recipientId),
            $chunkCount,
            $negotiatedMode,
        ));

        return [
            'transfer_id' => $transferId,
            'status' => 'awaiting_chunks',
            'negotiated_mode' => $negotiatedMode,
        ];
    }

    /**
     * Map a client's requested mode to the mode the server will actually
     * honour for this transfer.
     *
     * Rules (see docs/plans/streaming-improvement.md §gap 10–11):
     *   - missing / 'classic' → classic (default, old clients)
     *   - streaming disabled by operator knob → classic
     *   - recipient not seen in STREAM_ONLINE_WINDOW_SEC → classic
     *   - otherwise → streaming
     *
     * Unknown values (future-proofing against a typo in a new client)
     * get a 400 so we don't silently downgrade into classic.
     */
    private static function negotiateMode(Database $db, string $recipientId, ?string $requested): string
    {
        if ($requested === null || $requested === '' || $requested === 'classic') {
            return 'classic';
        }
        if ($requested !== 'streaming') {
            throw new ValidationError('Invalid mode');
        }
        if (!Config::streamingEnabled()) {
            return 'classic';
        }
        $recipient = (new DeviceRepository($db))->findById($recipientId);
        if ($recipient === null) {
            return 'classic';
        }
        $lastSeen = (int)($recipient['last_seen_at'] ?? 0);
        if (time() - $lastSeen > self::STREAM_ONLINE_WINDOW_SEC) {
            return 'classic';
        }
        return 'streaming';
    }

    public static function uploadChunk(
        Database $db,
        string $deviceId,
        string $transferId,
        int $chunkIndex,
        string $blobData,
    ): array {
        $transfers = new TransferRepository($db);
        $transfer = $transfers->findById($transferId);
        if (!$transfer) {
            throw new NotFoundError('Transfer not found');
        }
        if ($transfer['sender_id'] !== $deviceId) {
            throw new ForbiddenError('Not the sender of this transfer');
        }
        // Abort is terminal: sender's in-flight uploader keeps pushing
        // bytes until it's told to stop. Returning 410 here is how the
        // sender hears about a recipient-side abort. Checked before the
        // chunk-index validation so any chunk request against an aborted
        // transfer reports 410 uniformly.
        if ((int)($transfer['aborted'] ?? 0) === 1) {
            throw new AbortedError('Transfer has been aborted', $transfer['abort_reason'] ?? null);
        }
        if ($chunkIndex < 0 || $chunkIndex >= $transfer['chunk_count']) {
            throw new ValidationError('Invalid chunk_index');
        }
        if ($blobData === '') {
            throw new ValidationError('Empty chunk data');
        }

        $mode = $transfer['mode'] ?? 'classic';
        if ($mode === 'streaming') {
            // Mid-stream quota gate. Streaming init skipped the projected
            // reservation, so this is the only place the cap gets
            // enforced. The sum is across every undownloaded transfer to
            // the recipient — classic and streaming share the same
            // bucket.
            $currentBytes = (new ChunkRepository($db))->sumPendingBytesForRecipient($transfer['recipient_id']);
            $quotaBytes = Config::storageQuotaBytes();
            if ($currentBytes + strlen($blobData) > $quotaBytes) {
                AppLog::log('Transfer', sprintf(
                    'transfer.stream.waiting_quota transfer_id=%s chunk_index=%d current=%d cap=%d',
                    AppLog::shortId($transferId),
                    $chunkIndex,
                    $currentBytes,
                    $quotaBytes,
                ), 'warning');
                throw new StorageLimitError('Recipient storage limit exceeded');
            }
        }

        $storageDir = __DIR__ . '/../../storage/' . $transferId;
        if (!is_dir($storageDir)) {
            mkdir($storageDir, 0700, true);
        }
        // Atomic write: temp file + rename so a concurrent downloader cannot
        // observe a partially-written chunk (AES-GCM would fail auth on short bytes).
        $blobPath = $transferId . '/' . $chunkIndex . '.bin';
        $fullPath = __DIR__ . '/../../storage/' . $blobPath;
        $tmpPath = $fullPath . '.tmp';
        file_put_contents($tmpPath, $blobData);
        rename($tmpPath, $fullPath);

        $chunks = new ChunkRepository($db);
        $isNewChunk = !$chunks->chunkExists($transferId, $chunkIndex);
        if ($isNewChunk) {
            $chunks->insertChunk($transferId, $chunkIndex, $blobPath, strlen($blobData), time());
            $transfers->incrementChunksReceived($transferId);
            if ($mode === 'streaming') {
                $transfers->incrementChunksUploaded($transferId);
            }
            AppLog::log('Transfer', sprintf(
                'transfer.chunk.uploaded transfer_id=%s chunk_index=%d size=%d mode=%s',
                AppLog::shortId($transferId),
                $chunkIndex,
                strlen($blobData),
                $mode,
            ), 'debug');
        }

        $updated = $transfers->findById($transferId);
        if (!$updated) {
            throw new ApiError(500, 'Transfer row missing after chunk upload');
        }

        if ((int)$updated['chunks_received'] >= (int)$updated['chunk_count'] && (int)$updated['complete'] === 0) {
            $transfers->markComplete($transferId);
            $updated = $transfers->findById($transferId);
            if (!$updated) {
                throw new ApiError(500, 'Transfer row missing after completion update');
            }
        }

        $transition = TransferLifecycle::onChunkStored($transfer, $updated);
        $complete = $transition['is_complete'];

        // Streaming: fire `stream_ready` on the FIRST chunk stored.
        // markStreamReady is idempotent (WHERE stream_ready_at IS NULL),
        // so re-uploaded or concurrent chunks don't re-wake.
        if ($mode === 'streaming' && $isNewChunk) {
            if ($transfers->markStreamReady($transferId, time())) {
                AppLog::log('Transfer', sprintf(
                    'transfer.stream.ready transfer_id=%s sender=%s recipient=%s',
                    AppLog::shortId($transferId),
                    AppLog::shortId($transfer['sender_id']),
                    AppLog::shortId($transfer['recipient_id']),
                ));
                TransferWakeService::wakeStreamReady($db, $transferId);
            }
        }

        // Classic: fire `transfer_ready` on upload completion.
        // Streaming: recipient is already pulling, no second wake needed.
        if ($complete && (int)$transfer['complete'] === 0) {
            AppLog::log('Transfer', sprintf(
                'transfer.upload.completed transfer_id=%s sender=%s recipient=%s chunks=%d mode=%s',
                AppLog::shortId($transferId),
                AppLog::shortId($transfer['sender_id']),
                AppLog::shortId($transfer['recipient_id']),
                (int)$transfer['chunk_count'],
                $mode,
            ));
            if ($mode === 'classic') {
                TransferWakeService::wake($db, $transferId);
            }
        }

        return [
            'chunks_received' => $transition['chunks_received'],
            'complete' => $complete,
        ];
    }

    public static function listPending(Database $db, string $deviceId): array
    {
        return (new TransferRepository($db))->listPendingForRecipient($deviceId);
    }

    /** Returns the chunk's raw bytes. Controller emits them via Router::binary. */
    public static function downloadChunk(Database $db, string $deviceId, string $transferId, int $chunkIndex): string
    {
        $transfers = new TransferRepository($db);
        $transfer = $transfers->findById($transferId);
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            throw new NotFoundError('Transfer not found or not for you');
        }
        if ((int)($transfer['aborted'] ?? 0) === 1) {
            throw new AbortedError('Transfer has been aborted', $transfer['abort_reason'] ?? null);
        }

        $chunk = (new ChunkRepository($db))->findChunk($transferId, $chunkIndex);
        $mode = $transfer['mode'] ?? 'classic';
        if (!$chunk) {
            // Streaming: distinguish "not yet uploaded" from "gone".
            // chunks_downloaded is the highest-acked-+1; anything below
            // that was already served+acked+deleted (caller replayed) =
            // 410. Above: upstream just hasn't produced it yet = 425.
            if ($mode === 'streaming') {
                $chunksDownloaded = (int)($transfer['chunks_downloaded'] ?? 0);
                if ($chunkIndex < $chunksDownloaded) {
                    throw new AbortedError('Chunk already acknowledged and wiped');
                }
                throw new TooEarlyError(
                    'Chunk not yet uploaded',
                    self::STREAM_TOO_EARLY_RETRY_MS,
                );
            }
            throw new NotFoundError('Chunk not found');
        }

        $fullPath = __DIR__ . '/../../storage/' . $chunk['blob_path'];
        if (!file_exists($fullPath)) {
            throw new ApiError(500, 'Chunk file missing from storage');
        }

        if ($mode === 'streaming') {
            // Streaming does NOT advance chunks_downloaded on serve;
            // the recipient drives that via POST .../ack. The
            // cap-below-chunk_count trick from classic doesn't apply —
            // in streaming, progress is delete-on-ack, not cap-on-serve.
            $transfers->touchLastServedAt($transferId, time());
            AppLog::log('Transfer', sprintf(
                'transfer.chunk.served_and_pending_ack transfer_id=%s chunk_index=%d',
                AppLog::shortId($transferId),
                $chunkIndex,
            ), 'debug');
            return file_get_contents($fullPath);
        }

        // Classic: serve advances chunks_downloaded (capped below
        // chunk_count) so the sender's delivery tracker sees progress
        // even though the final "delivered" flip only happens on the
        // transfer-level ack.
        $newProgress = TransferLifecycle::recipientProgressTarget($transfer, $chunkIndex);
        $transfers->updateDownloadProgress($transferId, $newProgress);
        $updated = $transfers->findById($transferId);
        if (!$updated) {
            throw new ApiError(500, 'Transfer row missing after download progress update');
        }
        $transition = TransferLifecycle::onRecipientProgress($transfer, $updated);
        AppLog::log('Transfer', sprintf(
            'transfer.chunk.served transfer_id=%s chunk_index=%d progress=%d/%d',
            AppLog::shortId($transferId),
            $chunkIndex,
            $transition['next_progress'],
            (int)$transfer['chunk_count'],
        ), 'debug');

        return file_get_contents($fullPath);
    }

    public static function ack(Database $db, string $deviceId, string $transferId): array
    {
        $transfers = new TransferRepository($db);
        $transfer = $transfers->findById($transferId);
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            throw new NotFoundError('Transfer not found');
        }
        if ((int)($transfer['aborted'] ?? 0) === 1) {
            throw new AbortedError('Transfer has been aborted', $transfer['abort_reason'] ?? null);
        }
        // Pairing-stats SUM must run BEFORE chunk deletion (chunks table still holds sizes here).
        $senderId = $transfer['sender_id'];
        $totalBytes = (new ChunkRepository($db))->sumChunkBytesForTransfer($transferId);

        $ids = [$senderId, $deviceId];
        sort($ids);
        (new PairingRepository($db))->incrementPairingStats($ids[0], $ids[1], $totalBytes);

        TransferCleanupService::deleteChunkFilesAndRows($db, $transferId);

        // chunks_downloaded reaches chunk_count only here (on ack), not during serving.
        $transfers->markDelivered($transferId, time());
        $updated = $transfers->findById($transferId);
        if (!$updated) {
            throw new ApiError(500, 'Transfer row missing after delivery ACK');
        }
        TransferLifecycle::onAckReceived($transfer, $updated);
        AppLog::log('Delivery', sprintf(
            'delivery.acked transfer_id=%s recipient=%s total_bytes=%d',
            AppLog::shortId($transferId),
            AppLog::shortId($deviceId),
            $totalBytes,
        ));

        return ['status' => 'deleted'];
    }

    /**
     * Per-chunk ACK. Streaming only — deletes the chunk blob + row and
     * bumps `chunks_downloaded` to max(old, chunkIndex+1). When the
     * final chunk is ACK'd, flips `downloaded=1` / `delivered_at` and
     * credits pairing-stats with the transfer's total bytes before
     * deletion.
     *
     * Idempotent: re-ACK of a chunk already wiped is a no-op 200 so the
     * recipient's retry logic can replay without surprise. Classic
     * transfers explicitly reject this endpoint — the transfer-level
     * ack is the only legitimate path for them.
     */
    public static function ackChunk(Database $db, string $deviceId, string $transferId, int $chunkIndex): array
    {
        $transfers = new TransferRepository($db);
        $transfer = $transfers->findById($transferId);
        if (!$transfer || $transfer['recipient_id'] !== $deviceId) {
            throw new NotFoundError('Transfer not found');
        }
        if ((int)($transfer['aborted'] ?? 0) === 1) {
            throw new AbortedError('Transfer has been aborted', $transfer['abort_reason'] ?? null);
        }
        $mode = $transfer['mode'] ?? 'classic';
        if ($mode !== 'streaming') {
            throw new ValidationError('Per-chunk ack is only valid for streaming transfers');
        }
        $chunkCount = (int)$transfer['chunk_count'];
        if ($chunkIndex < 0 || $chunkIndex >= $chunkCount) {
            throw new ValidationError('Invalid chunk_index');
        }

        $chunks = new ChunkRepository($db);
        $chunk = $chunks->findChunk($transferId, $chunkIndex);
        if ($chunk !== null) {
            $fullPath = __DIR__ . '/../../storage/' . $chunk['blob_path'];
            if (file_exists($fullPath)) {
                unlink($fullPath);
            }
            $chunks->deleteChunkByIndex($transferId, $chunkIndex);
            AppLog::log('Transfer', sprintf(
                'transfer.chunk.acked_and_deleted transfer_id=%s chunk_index=%d',
                AppLog::shortId($transferId),
                $chunkIndex,
            ), 'debug');
        }

        $isFinalChunk = $chunkIndex === $chunkCount - 1;

        if ($isFinalChunk) {
            // Streaming may have ACK'd all earlier chunks already, so
            // sumChunkBytesForTransfer typically returns 0 here. That's
            // fine — the figure is only used to credit pairing stats.
            $totalBytes = $chunks->sumChunkBytesForTransfer($transferId);

            $senderId = $transfer['sender_id'];
            $ids = [$senderId, $deviceId];
            sort($ids);
            (new PairingRepository($db))->incrementPairingStats($ids[0], $ids[1], $totalBytes);

            TransferCleanupService::deleteChunkFilesAndRows($db, $transferId);
            $transfers->markDelivered($transferId, time());
            $updated = $transfers->findById($transferId);
            if (!$updated) {
                throw new ApiError(500, 'Transfer row missing after delivery ACK');
            }
            TransferLifecycle::onAckReceived($transfer, $updated);
            AppLog::log('Delivery', sprintf(
                'delivery.acked transfer_id=%s recipient=%s total_bytes=%d mode=streaming',
                AppLog::shortId($transferId),
                AppLog::shortId($deviceId),
                $totalBytes,
            ));
            return ['status' => 'delivered'];
        }

        // Intermediate ACK: bump chunks_downloaded (never regresses).
        // Cap below chunk_count so the invariant
        //   chunks_downloaded == chunk_count  ⇒  downloaded == 1
        // holds — the final-chunk branch above is the only path allowed
        // to reach chunk_count.
        $target = max((int)($transfer['chunks_downloaded'] ?? 0), $chunkIndex + 1);
        $cap = max(0, $chunkCount - 1);
        $transfers->updateDownloadProgress($transferId, min($target, $cap));
        $updated = $transfers->findById($transferId);
        if (!$updated) {
            throw new ApiError(500, 'Transfer row missing after chunk ACK');
        }
        TransferInvariants::assertValid($updated);

        return [
            'status' => 'acked',
            'chunk_index' => $chunkIndex,
            'chunks_downloaded' => (int)$updated['chunks_downloaded'],
        ];
    }

    /**
     * Either-party abort. Replaces the old sender-only `cancel` — both
     * endpoints still route here so old clients calling DELETE continue
     * to work; the new behaviour is that recipients can DELETE too.
     *
     * Reason values (passed from the controller):
     *   - 'sender_abort'     (sender cancelled)
     *   - 'sender_failed'    (sender gave up after retry exhaustion)
     *   - 'recipient_abort'  (recipient cancelled)
     *
     * Returns 404 for unknown transfer ids OR for transfers the caller
     * is neither sender nor recipient of — we deliberately do not
     * distinguish "wrong party" from "unknown id" to avoid leaking
     * transfer-id existence to third parties.
     */
    public static function abort(Database $db, string $deviceId, string $transferId, string $reason): array
    {
        $transfers = new TransferRepository($db);
        $transfer = $transfers->findById($transferId);
        if (!$transfer) {
            throw new NotFoundError('Transfer not found');
        }
        $isSender = $transfer['sender_id'] === $deviceId;
        $isRecipient = $transfer['recipient_id'] === $deviceId;
        if (!$isSender && !$isRecipient) {
            throw new NotFoundError('Transfer not found');
        }
        if (!in_array($reason, ['sender_abort', 'sender_failed', 'recipient_abort'], true)) {
            throw new ValidationError('Invalid abort reason');
        }
        // Cross-check reason against caller role so a recipient can't
        // file a sender-side reason (and vice versa).
        if ($isRecipient && $reason !== 'recipient_abort') {
            throw new ValidationError('Invalid abort reason for recipient');
        }
        if ($isSender && $reason === 'recipient_abort') {
            throw new ValidationError('Invalid abort reason for sender');
        }
        // Already-delivered transfers are terminal the other way — the
        // file was actually received. A late abort call is just a stale
        // client; treat as a no-op success so the DELETE endpoint stays
        // idempotent from the caller's perspective (matches how old
        // `cancel` behaved for already-gone transfers).
        if ((int)($transfer['downloaded'] ?? 0) === 1) {
            return [
                'status' => 'aborted',
                'reason' => $reason,
                'note' => 'already_delivered',
            ];
        }

        // markAborted's UPDATE is guarded by aborted=0, so concurrent
        // DELETE requests race cleanly: only one claim=true, the other
        // sees claim=false and skips the wake. Storage wipe is
        // idempotent so running it either way is safe.
        $claimed = $transfers->markAborted($transferId, $reason, time());
        TransferCleanupService::deleteChunkFilesAndRows($db, $transferId);

        if ($claimed) {
            $abortedBy = $isSender ? 'sender' : 'recipient';
            AppLog::log('Transfer', sprintf(
                'transfer.abort.%s transfer_id=%s sender=%s recipient=%s reason=%s',
                $abortedBy,
                AppLog::shortId($transferId),
                AppLog::shortId($transfer['sender_id']),
                AppLog::shortId($transfer['recipient_id']),
                $reason,
            ));
            TransferWakeService::wakeAbort($db, $transferId, $abortedBy);
        }

        return [
            'status' => 'aborted',
            'reason' => $reason,
        ];
    }

    /**
     * Back-compat alias so the sender-only DELETE path from before
     * streaming still reaches the unified abort. Controllers call this
     * when the caller is identified as the sender; the controller
     * routes recipient callers through `abort` directly.
     */
    public static function cancel(Database $db, string $deviceId, string $transferId): array
    {
        $result = self::abort($db, $deviceId, $transferId, 'sender_abort');
        // Preserve the exact on-wire shape of the previous response so
        // release-build clients that check for status=='cancelled' still
        // see what they expect.
        return ['status' => 'cancelled'] + $result;
    }
}
