package com.desktopconnector.crypto

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Pure-Kotlin tests for the `nextDefaultName` helper used by both the
 * pairing-confirm naming step (A.3) and the legacy-name migration (A.2).
 */
class NextDefaultNameTest {

    @Test fun `empty list suggests Desktop`() {
        assertEquals("Desktop", nextDefaultName(emptyList()))
    }

    @Test fun `Desktop taken suggests Desktop 2`() {
        assertEquals("Desktop 2", nextDefaultName(listOf("Desktop")))
    }

    @Test fun `Desktop and Desktop 2 taken suggests Desktop 3`() {
        assertEquals("Desktop 3", nextDefaultName(listOf("Desktop", "Desktop 2")))
    }

    @Test fun `gap in numbering takes the gap`() {
        assertEquals("Desktop 2", nextDefaultName(listOf("Desktop", "Desktop 3")))
    }

    @Test fun `comparison is case-insensitive`() {
        assertEquals("Desktop 2", nextDefaultName(listOf("desktop")))
        assertEquals("Desktop 2", nextDefaultName(listOf("DESKTOP")))
        assertEquals("Desktop 3", nextDefaultName(listOf("desktop", "DESKTOP 2")))
    }

    @Test fun `unrelated names are ignored`() {
        assertEquals("Desktop", nextDefaultName(listOf("Phone", "Tablet")))
    }

    @Test fun `Desktop free between unrelated names`() {
        assertEquals("Desktop", nextDefaultName(listOf("Phone", "Desktop 2", "Tablet")))
    }
}
