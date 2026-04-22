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
