package com.desktopconnector.network

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Per-key streak rules in `ConnectionManager.observeAuth`. Threshold is
 * 3-in-a-row before a key latches; counters reset on Success.
 *
 * Conventions: empty-string key is the canonical "global" — every
 * CREDENTIALS_INVALID lands there, so do 403s without peer attribution.
 */
class ConnectionManagerTest {

    private fun newCm() = ConnectionManager(serverUrl = "http://localhost")

    private fun cred(peerId: String? = null) =
        AuthObservation.Failure(AuthFailureKind.CREDENTIALS_INVALID, peerId)

    private fun pairing(peerId: String? = null) =
        AuthObservation.Failure(AuthFailureKind.PAIRING_MISSING, peerId)

    @Test fun `single failure does not latch`() {
        val cm = newCm()
        cm.observeAuth(cred())
        assertTrue(cm.authFailureByPeer.value.isEmpty())
    }

    @Test fun `three in a row latches the global key`() {
        val cm = newCm()
        repeat(3) { cm.observeAuth(cred()) }
        assertEquals(AuthFailureKind.CREDENTIALS_INVALID, cm.authFailureByPeer.value[""])
    }

    @Test fun `success resets the streak`() {
        val cm = newCm()
        cm.observeAuth(cred())
        cm.observeAuth(cred())
        cm.observeAuth(AuthObservation.Success)
        cm.observeAuth(cred())
        // Two ticks (one-then-success-then-one) — should not have latched.
        assertTrue(cm.authFailureByPeer.value.isEmpty())
    }

    @Test fun `failures across different keys do not collapse into one streak`() {
        val cm = newCm()
        // 1× CREDENTIALS_INVALID (global), 2× PAIRING_MISSING (peer X)
        cm.observeAuth(cred())
        cm.observeAuth(pairing(peerId = "X"))
        cm.observeAuth(pairing(peerId = "X"))
        // Neither (global, CRED) nor ("X", PAIRING) reached 3.
        assertTrue(cm.authFailureByPeer.value.isEmpty())
    }

    @Test fun `failures attributed to different peers do not collapse`() {
        val cm = newCm()
        cm.observeAuth(pairing(peerId = "X"))
        cm.observeAuth(pairing(peerId = "X"))
        cm.observeAuth(pairing(peerId = "Y"))
        cm.observeAuth(pairing(peerId = "Y"))
        // (X, PAIRING) = 2, (Y, PAIRING) = 2 — neither latches.
        assertTrue(cm.authFailureByPeer.value.isEmpty())
    }

    @Test fun `three PAIRING_MISSING for one peer latches just that peer`() {
        val cm = newCm()
        repeat(3) { cm.observeAuth(pairing(peerId = "X")) }
        assertEquals(AuthFailureKind.PAIRING_MISSING, cm.authFailureByPeer.value["X"])
        assertNull(cm.authFailureByPeer.value[""])
        assertNull(cm.authFailureByPeer.value["Y"])
    }

    @Test fun `clearAuthFailure removes one key without touching others`() {
        val cm = newCm()
        repeat(3) { cm.observeAuth(pairing(peerId = "X")) }
        repeat(3) { cm.observeAuth(pairing(peerId = "Y")) }
        assertEquals(2, cm.authFailureByPeer.value.size)

        cm.clearAuthFailure("X")

        assertNull(cm.authFailureByPeer.value["X"])
        assertEquals(AuthFailureKind.PAIRING_MISSING, cm.authFailureByPeer.value["Y"])
    }

    @Test fun `clearAuthFailure also resets the per-key streak counter`() {
        val cm = newCm()
        // Build a 2-streak under key X without latching.
        cm.observeAuth(pairing(peerId = "X"))
        cm.observeAuth(pairing(peerId = "X"))
        cm.clearAuthFailure("X")
        // After clear, a single subsequent failure must not immediately
        // re-latch (would be a regression — streak should restart at 1).
        cm.observeAuth(pairing(peerId = "X"))
        assertTrue(cm.authFailureByPeer.value.isEmpty())
    }

    @Test fun `null peerId on PAIRING_MISSING attributes to global key`() {
        val cm = newCm()
        repeat(3) { cm.observeAuth(pairing(peerId = null)) }
        assertEquals(AuthFailureKind.PAIRING_MISSING, cm.authFailureByPeer.value[""])
    }

    @Test fun `already-latched key does not bump on subsequent failures`() {
        val cm = newCm()
        repeat(3) { cm.observeAuth(pairing(peerId = "X")) }
        // After latch, more failures must be no-ops (don't re-fire,
        // don't bump anything; in particular no other key gets affected).
        repeat(5) { cm.observeAuth(pairing(peerId = "X")) }
        assertEquals(1, cm.authFailureByPeer.value.size)
        assertEquals(AuthFailureKind.PAIRING_MISSING, cm.authFailureByPeer.value["X"])
    }

    @Test fun `latch and clear round-trip empties the failure map`() {
        val cm = newCm()
        assertTrue(cm.authFailureByPeer.value.isEmpty())
        repeat(3) { cm.observeAuth(cred()) }
        assertFalse(cm.authFailureByPeer.value.isEmpty())
        cm.clearAuthFailure("")
        assertTrue(cm.authFailureByPeer.value.isEmpty())
    }
}
