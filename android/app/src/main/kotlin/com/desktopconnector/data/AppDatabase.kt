package com.desktopconnector.data

import android.content.Context
import androidx.room.*

class Converters {
    @TypeConverter
    fun fromStatus(status: TransferStatus): String = status.name
    @TypeConverter
    fun toStatus(value: String): TransferStatus = TransferStatus.valueOf(value)
    @TypeConverter
    fun fromDirection(dir: TransferDirection): String = dir.name
    @TypeConverter
    fun toDirection(value: String): TransferDirection = TransferDirection.valueOf(value)
}

@Database(entities = [QueuedTransfer::class], version = 8, exportSchema = false)
@TypeConverters(Converters::class)
abstract class AppDatabase : RoomDatabase() {
    abstract fun transferDao(): TransferDao

    companion object {
        @Volatile
        private var instance: AppDatabase? = null

        fun getInstance(context: Context): AppDatabase {
            return instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "desktop_connector.db"
                )
                    // `.addMigrations(...)` is consulted FIRST — Room uses the
                    // registered migration when source version matches. The
                    // `fallbackToDestructiveMigration()` below is the safety
                    // net for versions we haven't explicitly handled; once
                    // we're confident the migration chain is solid we can
                    // drop the fallback so future schema drift fails loudly
                    // instead of silently losing data.
                    .addMigrations(MIGRATION_7_8)
                    .fallbackToDestructiveMigration()
                    .build()
                    .also { instance = it }
            }
        }
    }
}
