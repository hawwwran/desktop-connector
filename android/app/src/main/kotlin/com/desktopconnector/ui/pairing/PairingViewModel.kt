package com.desktopconnector.ui.pairing

import android.app.Application
import android.util.Base64
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.desktopconnector.crypto.KeyManager
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.network.ApiClient
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
    val error: String? = null,
)

enum class PairingStage { SCANNING, VERIFYING, COMPLETE, ERROR }

class PairingViewModel(application: Application) : AndroidViewModel(application) {

    private val _state = MutableStateFlow(PairingState())
    val state: StateFlow<PairingState> = _state.asStateFlow()

    private val prefs = AppPreferences(application)
    private val keyManager = KeyManager(application)

    fun onQrScanned(serverUrl: String, desktopId: String, desktopPubkey: String, desktopName: String) {
        viewModelScope.launch(Dispatchers.IO) {
            try {
                // Save server URL
                prefs.serverUrl = serverUrl

                // Register if needed
                if (!prefs.isRegistered) {
                    val api = ApiClient(serverUrl)
                    val result = api.register(keyManager.publicKeyB64, "phone")
                        ?: throw Exception("Registration failed")
                    prefs.deviceId = result.getString("device_id")
                    prefs.authToken = result.getString("auth_token")
                }

                val api = ApiClient(serverUrl, prefs.deviceId!!, prefs.authToken!!)

                // Send pairing request
                if (!api.sendPairingRequest(desktopId, keyManager.publicKeyB64)) {
                    throw Exception("Failed to send pairing request")
                }

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

    fun confirmPairing() {
        val current = _state.value
        val sharedKey = keyManager.deriveSharedKey(current.desktopPubkey)

        keyManager.savePairedDevice(
            deviceId = current.desktopId,
            pubkeyB64 = current.desktopPubkey,
            symmetricKeyB64 = Base64.encodeToString(sharedKey, Base64.NO_WRAP),
            name = current.desktopName,
        )

        _state.value = current.copy(stage = PairingStage.COMPLETE)
    }

    fun cancel() {
        _state.value = PairingState(stage = PairingStage.SCANNING)
    }

    fun reset() {
        _state.value = PairingState(stage = PairingStage.SCANNING)
    }
}
