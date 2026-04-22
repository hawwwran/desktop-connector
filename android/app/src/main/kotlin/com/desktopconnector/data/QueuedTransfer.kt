package com.desktopconnector.data

import androidx.room.*

/**
 * Transfer status vocabulary.
 *
 * Classic values (present since v1): QUEUED, PREPARING, WAITING,
 * UPLOADING, COMPLETE, FAILED.
 *
 * Streaming values (added in D.2 — not yet written by any path until
 * D.3 / D.4a / D.4b wire them in):
 *   - SENDING         — sender has finished uploading OR is still
 *                       uploading while the recipient is already
 *                       draining chunks (overlapped streaming phase).
 *   - WAITING_STREAM  — mid-stream 507 backpressure; distinct from
 *                       classic WAITING which is init-time 507.
 *   - DELIVERING      — reserved for parity with desktop enum;
 *                       current Android streaming sender keeps
 *                       the row in SENDING until DELIVERED, so
 *                       this value is unused on Android today.
 *   - ABORTED         — terminal. Set by either side when DELETE /
 *                       410 observation flips the row.
 *
 * Readers that branch on `status` should cover the new values
 * (HomeScreen does; queries below do). Writers still only produce
 * classic values as of D.2.
 */
enum class TransferStatus {
    QUEUED, PREPARING, WAITING, UPLOADING, COMPLETE, FAILED,
    SENDING, WAITING_STREAM, DELIVERING, ABORTED,
}
enum class TransferDirection { OUTGOING, INCOMING }

@Entity(tableName = "queued_transfers")
data class QueuedTransfer(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val contentUri: String,
    val displayName: String,        // Protocol filename (.fn.clipboard.text, photo.jpg, etc.)
    val displayLabel: String = "",  // Human-readable ("Hello wor...", "Clipboard image", "photo.jpg")
    val mimeType: String,
    val sizeBytes: Long,
    val recipientDeviceId: String,
    val direction: TransferDirection = TransferDirection.OUTGOING,
    val status: TransferStatus = TransferStatus.QUEUED,
    val chunksUploaded: Int = 0,
    val totalChunks: Int = 0,
    // Delivery phase progress (outgoing only, after upload completes).
    // Owned by the 500ms DeliveryTracker — cleared when delivery logic starts/ends.
    val deliveryChunks: Int = 0,
    val deliveryTotal: Int = 0,
    val errorMessage: String? = null,
    val transferId: String? = null,
    val delivered: Boolean = false,
    val createdAt: Long = System.currentTimeMillis() / 1000,
    // --- Streaming fields (D.2). ---
    // Must match MIGRATION_7_8's ALTER TABLE shape. See Migrations.kt.
    //
    // `mode` is NOT NULL with a server-side DEFAULT so the migration
    // can backfill existing rows. @ColumnInfo(defaultValue) makes
    // Room's schema check align with the migration's DEFAULT clause.
    @ColumnInfo(defaultValue = "classic")
    val mode: String = "classic",
    // What the server negotiated (server may downgrade streaming →
    // classic). Null before init runs or for pre-streaming rows.
    val negotiatedMode: String? = null,
    // Set on DELETE / 410 observation: "sender_abort" | "sender_failed"
    // | "recipient_abort" | "devel:<…>" (from the _devel_ tool).
    val abortReason: String? = null,
    // Set when row ends in FAILED via a specific path:
    // "quota_timeout" (30 min WAITING_STREAM expiry) | "network"
    // (sender exhausted network-error budget) | etc. Rendered as
    // a parenthetical suffix in the status label.
    val failureReason: String? = null,
    // Epoch-ms when the row entered WAITING_STREAM. Used by the
    // 30 min zombie scrub in D.5. Null at any other time.
    val waitingStartedAt: Long? = null,
)

@Dao
interface TransferDao {
    @Insert
    suspend fun insert(transfer: QueuedTransfer): Long

    @Query("SELECT * FROM queued_transfers WHERE id = :id")
    suspend fun getById(id: Long): QueuedTransfer?

    @Query("SELECT COUNT(*) FROM queued_transfers WHERE id = :id")
    suspend fun exists(id: Long): Int

    @Query("SELECT * FROM queued_transfers WHERE transferId = :transferId AND direction = 'INCOMING' LIMIT 1")
    suspend fun getByTransferId(transferId: String): QueuedTransfer?

    @Query("SELECT * FROM queued_transfers ORDER BY createdAt DESC LIMIT 100")
    suspend fun getRecent(): List<QueuedTransfer>

    // "Active" outgoing rows — includes streaming in-flight statuses
    // (SENDING, WAITING_STREAM, DELIVERING) so the re-queue path
    // doesn't skip over rows that D.4a/b will start producing.
    // Classic statuses (QUEUED, PREPARING, WAITING, UPLOADING) kept
    // exactly as before.
    @Query("""
        SELECT * FROM queued_transfers
        WHERE status IN (
            'QUEUED', 'PREPARING', 'WAITING', 'UPLOADING',
            'SENDING', 'WAITING_STREAM', 'DELIVERING'
        )
          AND direction = 'OUTGOING'
        ORDER BY createdAt ASC
    """)
    suspend fun getPending(): List<QueuedTransfer>

    @Query("DELETE FROM queued_transfers WHERE id = :id")
    suspend fun delete(id: Long)

    @Query("UPDATE queued_transfers SET status = :status, errorMessage = :error WHERE id = :id")
    suspend fun updateStatus(id: Long, status: TransferStatus, error: String? = null)

    @Query("UPDATE queued_transfers SET chunksUploaded = :uploaded, totalChunks = :total WHERE id = :id")
    suspend fun updateProgress(id: Long, uploaded: Int, total: Int)

    @Query("UPDATE queued_transfers SET deliveryChunks = :downloaded, deliveryTotal = :total WHERE transferId = :transferId")
    suspend fun updateDeliveryProgress(transferId: String, downloaded: Int, total: Int)

    @Query("UPDATE queued_transfers SET deliveryChunks = 0, deliveryTotal = 0 WHERE transferId = :transferId")
    suspend fun clearDeliveryProgress(transferId: String)

    /**
     * Active-for-delivery-tracking outgoing transfers.
     *
     * Classic path (unchanged since v1): `status == COMPLETE AND delivered == 0`.
     * The upload finished, now we're waiting for the recipient to drain.
     *
     * Streaming path (D.4b): also include rows still in-flight on the
     * sender side — `UPLOADING` / `WAITING_STREAM` / `SENDING` — whenever
     * `negotiatedMode == 'streaming'`. The recipient drains overlappingly
     * with the sender's upload, so the tracker needs to paint
     * `deliveryChunks` while the sender is still producing chunks.
     *
     * Both branches share `delivered == 0` and a non-null `transferId`.
     */
    @Query("""
        SELECT transferId FROM queued_transfers
        WHERE direction = 'OUTGOING'
          AND delivered = 0
          AND transferId IS NOT NULL
          AND (
            status = 'COMPLETE'
            OR (negotiatedMode = 'streaming'
                AND status IN ('UPLOADING', 'WAITING_STREAM', 'SENDING'))
          )
    """)
    suspend fun getActiveDeliveryIds(): List<String>

    /**
     * Is this transfer's row streaming-negotiated? The delivery tracker
     * uses this to change stall-safeguard semantics: classic rows stall
     * out permanently after 2 min (tracker gives up), streaming rows
     * only clear their Y display (the sender may still be uploading).
     *
     * Returns null if the row doesn't exist or the mode column isn't set.
     */
    @Query("SELECT negotiatedMode FROM queued_transfers WHERE transferId = :transferId LIMIT 1")
    suspend fun getNegotiatedModeByTransferId(transferId: String): String?

    /**
     * Row-scoped read for the sender state machine (D.4b): inspect
     * `deliveryChunks` to decide whether to flip UPLOADING/WAITING_STREAM
     * → SENDING after a successful chunk upload. Tracker owns
     * `deliveryChunks` writes; the upload loop only reads.
     */
    @Query("SELECT deliveryChunks FROM queued_transfers WHERE id = :id LIMIT 1")
    suspend fun getDeliveryChunks(id: Long): Int?

    @Query("UPDATE queued_transfers SET status = :status, contentUri = :uri, displayLabel = :label, sizeBytes = :size WHERE id = :id")
    suspend fun completeDownload(id: Long, status: TransferStatus, uri: String, label: String, size: Long)

    @Query("UPDATE queued_transfers SET transferId = :transferId WHERE id = :id")
    suspend fun setTransferId(id: Long, transferId: String)

    @Query("UPDATE queued_transfers SET delivered = 1 WHERE transferId = :transferId")
    suspend fun markDelivered(transferId: String)

    @Query("SELECT transferId FROM queued_transfers WHERE direction = 'OUTGOING' AND delivered = 0 AND transferId IS NOT NULL AND status = 'COMPLETE'")
    suspend fun getUndeliveredTransferIds(): List<String>

    @Query("DELETE FROM queued_transfers WHERE id NOT IN (SELECT id FROM queued_transfers ORDER BY createdAt DESC LIMIT 100)")
    suspend fun trimHistory()

    // Clear history: delete every row that isn't still on the wire.
    // Mirrors `getPending()`'s active set so in-flight streaming rows
    // (SENDING, WAITING_STREAM, DELIVERING) survive a "Clear all".
    // Terminal rows — COMPLETE, FAILED, ABORTED — all get cleared.
    @Query("""
        DELETE FROM queued_transfers
        WHERE status NOT IN (
            'QUEUED', 'PREPARING', 'WAITING', 'UPLOADING',
            'SENDING', 'WAITING_STREAM', 'DELIVERING'
        )
    """)
    suspend fun clearAll()

    // --- Streaming DAO methods (D.2) ---
    //
    // Writers for these land in D.3 (recipient abort) and D.4a/b
    // (sender waiting + abort). Kept grouped so the pre-streaming
    // surface above stays reviewable in isolation.

    /**
     * Terminal-flip for either-party abort. Caller supplies the reason
     * from {sender_abort, sender_failed, recipient_abort}; the server's
     * DELETE side plus the 410 Gone observation path both end up here.
     */
    @Query("UPDATE queued_transfers SET status = 'ABORTED', abortReason = :reason WHERE id = :id")
    suspend fun markAborted(id: Long, reason: String)

    /**
     * Stamp a row into WAITING_STREAM with the current epoch-ms. The
     * 30 min zombie scrub in D.5 reads `waitingStartedAt` to decide
     * when a stuck row flips to FAILED with `failure_reason=quota_timeout`.
     */
    @Query("UPDATE queued_transfers SET status = 'WAITING_STREAM', waitingStartedAt = :startedAt WHERE id = :id")
    suspend fun markWaitingStream(id: Long, startedAt: Long)

    /**
     * Query for the D.5 scrub pass: rows stuck in WAITING_STREAM past
     * `threshold` epoch-ms. Null `waitingStartedAt` excluded so rows
     * that got here via a migration bug (shouldn't happen) aren't
     * incorrectly flipped.
     */
    @Query("SELECT * FROM queued_transfers WHERE status = 'WAITING_STREAM' AND waitingStartedAt IS NOT NULL AND waitingStartedAt < :threshold")
    suspend fun getStaleWaitingStream(threshold: Long): List<QueuedTransfer>

    /**
     * Bulk update used by the sender state machine (D.4a) when a row
     * transitions out of WAITING_STREAM on a successful next chunk:
     * status → UPLOADING (or SENDING, D.4b) and waitingStartedAt
     * clears so the scrub doesn't see a stale stamp.
     */
    @Query("UPDATE queued_transfers SET status = :status, waitingStartedAt = NULL WHERE id = :id")
    suspend fun clearWaitingStream(id: Long, status: TransferStatus)

    /**
     * Stamp the negotiated mode the server accepted. D.4a calls this
     * right after a successful `init` with `mode="streaming"`; classic
     * path doesn't need to call it (default stays `mode="classic"`,
     * `negotiatedMode=null`).
     */
    @Query("UPDATE queued_transfers SET mode = :mode, negotiatedMode = :negotiatedMode WHERE id = :id")
    suspend fun setNegotiatedMode(id: Long, mode: String, negotiatedMode: String?)

    /**
     * Stamp a FAILED row with its reason code. Used when the
     * 30 min WAITING_STREAM window expires (`quota_timeout`) or the
     * sender exhausts its network-error budget (`network`).
     */
    @Query("UPDATE queued_transfers SET status = 'FAILED', failureReason = :reason, errorMessage = :reason WHERE id = :id")
    suspend fun markFailedWithReason(id: Long, reason: String)
}
