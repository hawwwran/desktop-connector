package com.desktopconnector.data

import androidx.room.*

enum class TransferStatus { QUEUED, PREPARING, WAITING, UPLOADING, COMPLETE, FAILED }
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

    @Query("SELECT * FROM queued_transfers WHERE status IN ('QUEUED', 'PREPARING', 'WAITING', 'UPLOADING') AND direction = 'OUTGOING' ORDER BY createdAt ASC")
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

    @Query("SELECT transferId FROM queued_transfers WHERE direction = 'OUTGOING' AND status = 'COMPLETE' AND delivered = 0 AND transferId IS NOT NULL")
    suspend fun getActiveDeliveryIds(): List<String>

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

    @Query("DELETE FROM queued_transfers WHERE status NOT IN ('QUEUED', 'PREPARING', 'WAITING', 'UPLOADING')")
    suspend fun clearAll()
}
