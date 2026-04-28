package com.desktopconnector.util

import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files

class UpdateCacheCleanupTest {

    private lateinit var dir: File

    @Before fun setUp() {
        dir = Files.createTempDirectory("update-cleanup-test").toFile()
    }

    @After fun tearDown() {
        dir.deleteRecursively()
    }

    @Test fun `prune deletes files older than threshold and keeps recent ones`() {
        val tenDaysAgo = System.currentTimeMillis() - 10L * 24 * 60 * 60 * 1000
        val oneHourAgo = System.currentTimeMillis() - 60L * 60 * 1000

        val old = File(dir, "old.apk").apply {
            writeBytes(byteArrayOf())
            setLastModified(tenDaysAgo)
        }
        val recent = File(dir, "recent.apk").apply {
            writeBytes(byteArrayOf())
            setLastModified(oneHourAgo)
        }

        UpdateCacheCleanup.pruneOldUpdates(dir, maxAgeDays = 7)

        assertFalse("Old file should be deleted", old.exists())
        assertTrue("Recent file should remain", recent.exists())
    }

    @Test fun `prune is no-op when directory does not exist`() {
        val missing = File(dir, "nonexistent")
        // Should not throw.
        UpdateCacheCleanup.pruneOldUpdates(missing)
        assertFalse(missing.exists())
    }

    @Test fun `prune is no-op on empty directory`() {
        UpdateCacheCleanup.pruneOldUpdates(dir, maxAgeDays = 7)
        assertTrue(dir.isDirectory)
    }
}
