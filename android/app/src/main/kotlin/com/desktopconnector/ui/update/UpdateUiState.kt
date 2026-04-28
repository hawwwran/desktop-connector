package com.desktopconnector.ui.update

import com.desktopconnector.network.UpdateInfo

/**
 * UI states for the auto-update flow. Drives both the Settings entry
 * subtitle and the modal contents.
 */
sealed interface UpdateUiState {
    /** No info yet (haven't checked, or VM just constructed). */
    data object Idle : UpdateUiState

    /** Latest from server isn't newer than the running version. */
    data class NoUpdate(val currentVersion: String) : UpdateUiState

    /** Newer release exists. `dismissed = true` when the user previously
     *  tapped "Skip this version" for `info.latestVersion`. */
    data class Available(val info: UpdateInfo, val dismissed: Boolean) : UpdateUiState

    /** Force-check in flight. `previous` is what to revert to if cancelled. */
    data class Checking(val previous: UpdateUiState) : UpdateUiState

    /** APK download in progress. `progress` is 0f..1f; 0f when total is unknown. */
    data class Downloading(
        val info: UpdateInfo,
        val progress: Float,
        val bytesRead: Long,
        val total: Long,
    ) : UpdateUiState

    /** APK ready, system installer intent fired. Modal closes shortly. */
    data class Launching(val info: UpdateInfo) : UpdateUiState

    /** Network error or other failure. */
    data class Error(val message: String) : UpdateUiState
}
