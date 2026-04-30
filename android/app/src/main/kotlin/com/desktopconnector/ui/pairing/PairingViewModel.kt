package com.desktopconnector.ui.pairing

import android.app.Application
import android.util.Base64
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.crypto.PairingRepository
import com.desktopconnector.crypto.nextDefaultName
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.network.ApiClient
import com.desktopconnector.network.FcmManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class PairingState(
    val stage: PairingStage = PairingStage.SCANNING,
    val verificationCode: String? = null,
    val desktopId: String = "",
    val desktopPubkey: String = "",
    val desktopName: String = "",
    val serverUrl: String = "",
    /** Pre-filled value for the NAMING text field — "Desktop" or
     *  "Desktop N" depending on what's already taken. */
    val suggestedName: String = "",
    val error: String? = null,
)

enum class PairingStage { SCANNING, SENDING, VERIFYING, NAMING, COMPLETE, ERROR }

class PairingViewModel(application: Application) : AndroidViewModel(application) {

    private val _state = MutableStateFlow(PairingState())
    val state: StateFlow<PairingState> = _state.asStateFlow()

    private val prefs = AppPreferences(application)
    private val keyManager = KeyManager(application)
    private val pairingRepo = PairingRepository.getInstance(application)

    fun onQrScanned(serverUrl: String, desktopId: String, desktopPubkey: String, desktopName: String) {
        AppLog.log("Pairing", "pairing.qr.scanned desktop_id=${desktopId.take(12)}")
        // Flip immediately so the UI stops the scanner and shows a spinner
        // while we register + send the pairing request over the network.
        _state.value = PairingState(stage = PairingStage.SENDING)
        viewModelScope.launch(Dispatchers.IO) {
            try {
                // Save server URL
                prefs.serverUrl = serverUrl

                // Stale-credentials guard. If we hold stored creds but have
                // no paired device, the creds are leftover from a previous
                // install — most often Android Auto Backup restoring
                // `dc_prefs.xml` while `dc_secure_keys.xml` (the keypair)
                // is correctly excluded, leaving the local identity
                // mismatched against the server's records. Drop them so
                // the registration block below re-registers cleanly
                // instead of sending an authed pairing request that the
                // server 401s.
                if (pairingRepo.pairs.value.isEmpty() && prefs.isRegistered) {
                    AppLog.log("Pairing", "pairing.startup.stale_creds_cleared")
                    prefs.clearAuthCredentials()
                }

                // Register if needed
                if (!prefs.isRegistered) {
                    val api = ApiClient(serverUrl)
                    val result = api.register(keyManager.publicKeyB64, "phone")
                        ?: throw Exception("Registration failed")
                    prefs.deviceId = result.getString("device_id")
                    prefs.authToken = result.getString("auth_token")
                    AppLog.log("Pairing", "startup.device.registered device_id=${prefs.deviceId?.take(12)}")
                }

                val api = ApiClient(serverUrl, prefs.deviceId!!, prefs.authToken!!)

                // Send pairing request
                if (!api.sendPairingRequest(desktopId, keyManager.publicKeyB64)) {
                    AppLog.log("Pairing", "pairing.request.sent desktop_id=${desktopId.take(12)} outcome=failed", "error")
                    throw Exception("Failed to send pairing request")
                }
                AppLog.log("Pairing", "pairing.request.sent desktop_id=${desktopId.take(12)} outcome=succeeded")

                // Derive shared key and verification code
                val sharedKey = keyManager.deriveSharedKey(desktopPubkey)
                val code = keyManager.getVerificationCode(sharedKey)

                _state.value = PairingState(
                    stage = PairingStage.VERIFYING,
                    verificationCode = code,
                    desktopId = desktopId,
                    desktopPubkey = desktopPubkey,
                    desktopName = desktopName,
                    serverUrl = serverUrl,
                )
            } catch (e: Exception) {
                _state.value = PairingState(
                    stage = PairingStage.ERROR,
                    error = e.message,
                )
            }
        }
    }

    /** Codes-match → advance to the naming step. The actual save lands
     *  in `commitName` so the user owns the user-visible label.
     *  Suggestion: the QR-supplied `desktopName` (the desktop's hostname),
     *  falling back to "Desktop"/"Desktop N" only when the QR didn't
     *  carry one. */
    fun confirmPairing() {
        val current = _state.value
        val suggested = current.desktopName.trim().ifBlank {
            nextDefaultName(pairingRepo.pairs.value.map { it.name })
        }
        _state.value = current.copy(
            stage = PairingStage.NAMING,
            suggestedName = suggested,
        )
    }

    /** Persist the pair under the user-chosen name and finalize. Blank
     *  input falls back to the suggestion (no empty-name pairs). */
    fun commitName(name: String) {
        val current = _state.value
        val finalName = name.trim().ifBlank { current.suggestedName }
        val sharedKey = keyManager.deriveSharedKey(current.desktopPubkey)

        keyManager.savePairedDevice(
            deviceId = current.desktopId,
            pubkeyB64 = current.desktopPubkey,
            symmetricKeyB64 = Base64.encodeToString(sharedKey, Base64.NO_WRAP),
            name = finalName,
        )
        pairingRepo.refresh()
        pairingRepo.selectPair(current.desktopId)

        _state.value = PairingState(stage = PairingStage.COMPLETE)
        AppLog.log("Pairing", "pairing.confirm.accepted peer=${current.desktopId.take(12)}")

        // Trigger FCM init now that we have a paired device. Pass prefs so
        // the cached FCM token is cleared — otherwise the next
        // registerToken() would skip the POST because the token string
        // is unchanged, and the new server record would never learn it.
        FcmManager.reset(prefs)
    }

    /** Back-arrow from NAMING — preserves the verification code so the
     *  user can re-confirm without re-scanning. */
    fun cancelNaming() {
        _state.value = _state.value.copy(stage = PairingStage.VERIFYING)
    }

    fun cancel() {
        _state.value = PairingState(stage = PairingStage.SCANNING)
    }

    fun reset() {
        _state.value = PairingState(stage = PairingStage.SCANNING)
    }
}
