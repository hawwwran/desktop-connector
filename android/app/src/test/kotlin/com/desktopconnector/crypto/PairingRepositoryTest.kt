package com.desktopconnector.crypto

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-Kotlin tests for `PairingRepository.unpair`'s selection-fallback
 * rule. Constructed via the internal constructor with fakes — no Android
 * runtime needed.
 */
class PairingRepositoryTest {

    private fun pair(id: String, name: String, pairedAt: Long) =
        PairedDeviceInfo(id, "pubkey-$id", "symkey-$id", name, pairedAt)

    private class FakeStore(initial: List<PairedDeviceInfo>) : PairedDeviceStore {
        private val byId = initial.associateBy { it.deviceId }.toMutableMap()
        override fun getAllPairedDevices(): List<PairedDeviceInfo> = byId.values.toList()
        override fun removePairedDevice(deviceId: String) { byId.remove(deviceId) }
        override fun setPairedDeviceName(deviceId: String, name: String) {
            byId[deviceId]?.let { byId[deviceId] = it.copy(name = name) }
        }
    }

    private class FakePref(initial: String? = null) : SelectedPairPref {
        override var selectedDeviceId: String? = initial
    }

    @Test fun `unpair the selected pair falls over to most-recently-paired remaining`() {
        // pairedAt: A=100, B=200, C=300. C is most recent.
        val store = FakeStore(listOf(
            pair("A", "Desktop", 100),
            pair("B", "Phone", 200),
            pair("C", "Tablet", 300),
        ))
        val repo = PairingRepository(store, FakePref(initial = "B"))

        repo.unpair("B")

        assertEquals(listOf("C", "A"), repo.pairs.value.map { it.deviceId })
        assertEquals("C", repo.selectedDeviceId.value)
    }

    @Test fun `unpair a non-selected pair leaves selection alone`() {
        val store = FakeStore(listOf(
            pair("A", "Desktop", 100),
            pair("B", "Phone", 200),
            pair("C", "Tablet", 300),
        ))
        val repo = PairingRepository(store, FakePref(initial = "C"))

        repo.unpair("A")

        assertEquals(listOf("C", "B"), repo.pairs.value.map { it.deviceId })
        assertEquals("C", repo.selectedDeviceId.value)
    }

    @Test fun `unpair last remaining pair clears selection`() {
        val store = FakeStore(listOf(pair("A", "Desktop", 100)))
        val repo = PairingRepository(store, FakePref(initial = "A"))

        repo.unpair("A")

        assertTrue(repo.pairs.value.isEmpty())
        assertNull(repo.selectedDeviceId.value)
    }

    @Test fun `unpair selected when no other pairs leaves selection null`() {
        val store = FakeStore(listOf(pair("A", "Desktop", 100)))
        val repo = PairingRepository(store, FakePref(initial = "A"))

        repo.unpair("A")

        assertNull(repo.selectedDeviceId.value)
    }

    @Test fun `unpair does nothing for an unknown device id`() {
        val store = FakeStore(listOf(
            pair("A", "Desktop", 100),
            pair("B", "Phone", 200),
        ))
        val pref = FakePref(initial = "A")
        val repo = PairingRepository(store, pref)

        repo.unpair("Z")

        assertEquals(2, repo.pairs.value.size)
        assertEquals("A", repo.selectedDeviceId.value)
    }

    @Test fun `rename updates the pairs flow`() {
        val store = FakeStore(listOf(pair("A", "Desktop", 100)))
        val repo = PairingRepository(store, FakePref())

        repo.rename("A", "Office Desktop")

        assertEquals("Office Desktop", repo.pairs.value.single().name)
    }

    @Test fun `pairs are sorted most-recently-paired first`() {
        val store = FakeStore(listOf(
            pair("A", "Old", 100),
            pair("C", "New", 300),
            pair("B", "Mid", 200),
        ))
        val repo = PairingRepository(store, FakePref())

        assertEquals(listOf("C", "B", "A"), repo.pairs.value.map { it.deviceId })
    }
}
