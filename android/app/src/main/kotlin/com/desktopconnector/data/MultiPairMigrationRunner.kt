package com.desktopconnector.data

import android.content.Context
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.crypto.PairingRepository
import com.desktopconnector.crypto.nextDefaultName

/**
 * One-shot install-level cleanup for the multi-pair switch:
 *   1. Rename empty/`Unknown` pair names to `Desktop` / `Desktop N`.
 *   2. Reassign legacy INCOMING rows whose `peerDeviceId` is the
 *      phone's own id (the pre-rename bug) to the most-recently-paired
 *      peer — best-effort, read-only history rows.
 *
 * Gated by `AppPreferences.multiPairMigrationDone`; runs off-main.
 */
object MultiPairMigrationRunner {

    suspend fun runIfNeeded(context: Context) {
        val prefs = AppPreferences(context)
        if (prefs.multiPairMigrationDone) return

        val keyManager = KeyManager(context)
        renameLegacyUnnamedPairs(keyManager)
        backfillLegacyIncomingPeers(context, prefs, keyManager)

        prefs.multiPairMigrationDone = true
        // Close the race with the singleton's first construction: if
        // PairingRepository was instantiated before the rename pass ran,
        // its cached _pairs holds pre-rename names. Force a re-read.
        PairingRepository.getInstance(context).refresh()
        AppLog.log("App", "multipair.migration.done")
    }

    private fun renameLegacyUnnamedPairs(keyManager: KeyManager) {
        val pairs = keyManager.getAllPairedDevices()
        val needsRename = pairs.filter { it.name.isBlank() || it.name == "Unknown" }
        if (needsRename.isEmpty()) return

        val takenNames = pairs
            .filter { it.name.isNotBlank() && it.name != "Unknown" }
            .map { it.name }
            .toMutableList()
        for (pair in needsRename) {
            val newName = nextDefaultName(takenNames)
            keyManager.setPairedDeviceName(pair.deviceId, newName)
            takenNames.add(newName)
        }
    }

    private suspend fun backfillLegacyIncomingPeers(
        context: Context,
        prefs: AppPreferences,
        keyManager: KeyManager,
    ) {
        val ownDeviceId = prefs.deviceId ?: return
        val mostRecentPeer = keyManager.getAllPairedDevices()
            .maxByOrNull { it.pairedAt } ?: return
        if (mostRecentPeer.deviceId == ownDeviceId) return // pathological, skip

        val db = AppDatabase.getInstance(context)
        db.transferDao().reassignIncomingPeer(ownDeviceId, mostRecentPeer.deviceId)
    }
}
