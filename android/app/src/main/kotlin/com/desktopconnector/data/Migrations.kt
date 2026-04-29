package com.desktopconnector.data

import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

/**
 * Manual Room migrations.
 *
 * Phase D on Android is our first user-facing migration — pre-D the
 * DB was carried forward by `fallbackToDestructiveMigration()`, which
 * silently drops data on any mismatch. We keep the fallback as a
 * safety net, but every version bump from here on ships an explicit
 * `Migration` so upgrades preserve paired-device history.
 */

/**
 * v7 → v8 — streaming-relay fields on `queued_transfers`.
 *
 * Five additive columns so D.3 (recipient streaming loop) and D.4a/b
 * (sender state machine + tracker) have schema to write into.
 * Nothing in v8 writes the new columns yet; D.3/D.4 wire them in.
 *
 *   - mode              TEXT NOT NULL DEFAULT 'classic'
 *                       — what the sender requested; existing rows
 *                         were all classic-era so the default is safe.
 *                       — Entity uses @ColumnInfo(defaultValue = "classic")
 *                         so Room's schema check matches this DEFAULT.
 *   - negotiatedMode    TEXT NULL
 *                       — what the server accepted (may downgrade);
 *                         null means "init hasn't run or pre-streaming".
 *   - abortReason       TEXT NULL
 *                       — filled by DELETE / 410 observation.
 *   - failureReason     TEXT NULL
 *                       — "quota_timeout" / "network" / etc.
 *   - waitingStartedAt  INTEGER NULL
 *                       — epoch-ms when row entered WAITING_STREAM;
 *                         used by the zombie scrub in D.5.
 *
 * The migration is write-only ALTER TABLE — no data transforms,
 * no index changes. Idempotent in practice because SQLite rejects
 * ALTER TABLE ADD COLUMN on a pre-existing column, and the Room
 * version gate stops this from running more than once anyway.
 */
val MIGRATION_7_8 = object : Migration(7, 8) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL("ALTER TABLE queued_transfers ADD COLUMN mode TEXT NOT NULL DEFAULT 'classic'")
        db.execSQL("ALTER TABLE queued_transfers ADD COLUMN negotiatedMode TEXT")
        db.execSQL("ALTER TABLE queued_transfers ADD COLUMN abortReason TEXT")
        db.execSQL("ALTER TABLE queued_transfers ADD COLUMN failureReason TEXT")
        db.execSQL("ALTER TABLE queued_transfers ADD COLUMN waitingStartedAt INTEGER")
    }
}

/**
 * v8 → v9 — rename `recipientDeviceId` → `peerDeviceId`. SQLite < 3.25
 * lacks RENAME COLUMN, so we do the table-rebuild dance. The CREATE
 * TABLE schema must match Room's generated v9 byte-for-byte: PRAGMA
 * validation is strict about the `notnull` flag (explicit NOT NULL on
 * the AUTOINCREMENT id is required) and DEFAULT clauses. Diff against
 * `app/schemas/.../9.json` if validation rejects.
 *
 * Idempotent on source column: a prior failed run that left the table
 * half-migrated (peerDeviceId present, version pragma still 8) is
 * recoverable.
 */
val MIGRATION_8_9 = object : Migration(8, 9) {
    override fun migrate(db: SupportSQLiteDatabase) {
        val sourceCol = if (hasColumn(db, "queued_transfers", "peerDeviceId"))
            "peerDeviceId" else "recipientDeviceId"

        db.execSQL(
            """
            CREATE TABLE queued_transfers_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                contentUri TEXT NOT NULL,
                displayName TEXT NOT NULL,
                displayLabel TEXT NOT NULL,
                mimeType TEXT NOT NULL,
                sizeBytes INTEGER NOT NULL,
                peerDeviceId TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                chunksUploaded INTEGER NOT NULL,
                totalChunks INTEGER NOT NULL,
                deliveryChunks INTEGER NOT NULL,
                deliveryTotal INTEGER NOT NULL,
                errorMessage TEXT,
                transferId TEXT,
                delivered INTEGER NOT NULL,
                createdAt INTEGER NOT NULL,
                mode TEXT NOT NULL DEFAULT 'classic',
                negotiatedMode TEXT,
                abortReason TEXT,
                failureReason TEXT,
                waitingStartedAt INTEGER
            )
            """.trimIndent()
        )
        db.execSQL(
            """
            INSERT INTO queued_transfers_new (
                id, contentUri, displayName, displayLabel, mimeType, sizeBytes,
                peerDeviceId, direction, status, chunksUploaded, totalChunks,
                deliveryChunks, deliveryTotal, errorMessage, transferId, delivered,
                createdAt, mode, negotiatedMode, abortReason, failureReason,
                waitingStartedAt
            )
            SELECT
                id, contentUri, displayName, displayLabel, mimeType, sizeBytes,
                $sourceCol, direction, status, chunksUploaded, totalChunks,
                deliveryChunks, deliveryTotal, errorMessage, transferId, delivered,
                createdAt, mode, negotiatedMode, abortReason, failureReason,
                waitingStartedAt
            FROM queued_transfers
            """.trimIndent()
        )
        db.execSQL("DROP TABLE queued_transfers")
        db.execSQL("ALTER TABLE queued_transfers_new RENAME TO queued_transfers")
    }

    private fun hasColumn(db: SupportSQLiteDatabase, table: String, column: String): Boolean {
        db.query("PRAGMA table_info(`$table`)").use { cursor ->
            val nameIdx = cursor.getColumnIndex("name")
            while (cursor.moveToNext()) {
                if (cursor.getString(nameIdx) == column) return true
            }
        }
        return false
    }
}
