package com.desktopconnector.crypto

import android.content.Context
import com.desktopconnector.data.AppPreferences
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn

/**
 * The slice of `KeyManager` that `PairingRepository` reads. Extracted as
 * an interface so JVM unit tests can stand in a fake without needing
 * EncryptedSharedPreferences.
 */
interface PairedDeviceStore {
    fun getAllPairedDevices(): List<PairedDeviceInfo>
    fun removePairedDevice(deviceId: String)
    fun setPairedDeviceName(deviceId: String, name: String)
}

/** Same idea for the persisted-selection side of `AppPreferences`. */
interface SelectedPairPref {
    var selectedDeviceId: String?
}

/**
 * Process-singleton reactive view over `KeyManager`'s paired-desktop blobs.
 * `KeyManager` doesn't emit on save/remove, so any mutation site must call
 * `refresh()` for the flows to update.
 */
class PairingRepository internal constructor(
    private val keyManager: PairedDeviceStore,
    private val prefs: SelectedPairPref,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private val _pairs = MutableStateFlow(loadPairs())
    val pairs: StateFlow<List<PairedDeviceInfo>> = _pairs.asStateFlow()

    private val _selectedDeviceId = MutableStateFlow(prefs.selectedDeviceId)
    val selectedDeviceId: StateFlow<String?> = _selectedDeviceId.asStateFlow()

    val selected: StateFlow<PairedDeviceInfo?> =
        combine(_pairs, _selectedDeviceId) { pairs, id ->
            pairs.firstOrNull { it.deviceId == id } ?: pairs.firstOrNull()
        }.stateIn(
            scope,
            SharingStarted.Eagerly,
            _pairs.value.firstOrNull { it.deviceId == _selectedDeviceId.value }
                ?: _pairs.value.firstOrNull(),
        )

    val isPaired: StateFlow<Boolean> = _pairs
        .map { it.isNotEmpty() }
        .stateIn(scope, SharingStarted.Eagerly, _pairs.value.isNotEmpty())

    /** Re-read from KeyManager. Cheap (a SharedPreferences read).
     *  Idempotent — emits only if the snapshot changed. */
    fun refresh() {
        val next = loadPairs()
        if (next != _pairs.value) _pairs.value = next
    }

    /** Persist + emit a new selection. `null` clears the explicit choice;
     *  `selected` falls back to the most-recently-paired entry. */
    fun selectPair(deviceId: String?) {
        if (_selectedDeviceId.value == deviceId) return
        prefs.selectedDeviceId = deviceId
        _selectedDeviceId.value = deviceId
    }

    fun rename(deviceId: String, newName: String) {
        keyManager.setPairedDeviceName(deviceId, newName)
        refresh()
    }

    /** Keystore + selection only. The `.fn.unpair` send and per-peer
     *  history wipe are orchestrated by `TransferViewModel.unpairDesktop`,
     *  which has the API client and DB. If the removed pair was selected,
     *  fall over to the most-recently-paired remaining entry. */
    fun unpair(deviceId: String) {
        val wasSelected = _selectedDeviceId.value == deviceId
        keyManager.removePairedDevice(deviceId)
        refresh()
        if (wasSelected) selectPair(_pairs.value.firstOrNull()?.deviceId)
    }

    private fun loadPairs(): List<PairedDeviceInfo> =
        keyManager.getAllPairedDevices().sortedByDescending { it.pairedAt }

    companion object {
        @Volatile private var instance: PairingRepository? = null

        fun getInstance(context: Context): PairingRepository {
            return instance ?: synchronized(this) {
                instance ?: PairingRepository(
                    KeyManager(context.applicationContext),
                    AppPreferences(context.applicationContext),
                ).also { instance = it }
            }
        }
    }
}

/**
 * "Desktop" if free, else "Desktop 2", "Desktop 3", … Comparison is
 * case-insensitive ("desktop" collides with "Desktop"). Used by the
 * pairing-confirm naming step (A.3) and the legacy-name migration (A.2).
 */
fun nextDefaultName(existing: List<String>): String {
    val taken = existing.map { it.lowercase() }.toSet()
    if ("desktop" !in taken) return "Desktop"
    var i = 2
    while (true) {
        val candidate = "Desktop $i"
        if (candidate.lowercase() !in taken) return candidate
        i++
    }
}

