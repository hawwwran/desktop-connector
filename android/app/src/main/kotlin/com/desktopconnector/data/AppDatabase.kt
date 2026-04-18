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

@Database(entities = [QueuedTransfer::class], version = 7, exportSchema = false)
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
                ).fallbackToDestructiveMigration().build().also { instance = it }
            }
        }
    }
}
