package com.desktopconnector.ui.update

import android.app.Application
import android.content.pm.ApplicationInfo
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.AppPreferences
import com.desktopconnector.network.UpdateChecker
import com.desktopconnector.network.UpdateDownloader
import com.desktopconnector.util.Installer
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Activity-scoped ViewModel for the auto-update flow.
 *
 * Two cancellation domains:
 * - `checkJob` — version-check requests. Cancelled when the activity
 *   backgrounds (`MainActivity.onStop` calls `cancelInFlightCheck`).
 * - `downloadJob` — APK download. NOT cancelled on minimize. Only the
 *   user's explicit Cancel button stops it; the downloader's
 *   PARTIAL_WAKE_LOCK keeps the CPU alive while the screen is off.
 */
class UpdateViewModel(application: Application) : AndroidViewModel(application) {

    private val app: Application = application
    private val prefs = AppPreferences(application)

    private val currentVersion: String = run {
        try {
            application.packageManager
                .getPackageInfo(application.packageName, 0)
                .versionName ?: "0.0.0"
        } catch (_: Exception) { "0.0.0" }
    }

    private val isDebugBuild: Boolean =
        (application.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0

    private val checker = UpdateChecker(
        cacheFile = File(application.cacheDir, "update-check.json"),
        currentVersion = currentVersion,
        isDebugBuild = isDebugBuild,
    )
    private val downloader = UpdateDownloader.forContext(application)

    private val _state = MutableStateFlow<UpdateUiState>(UpdateUiState.Idle)
    val state: StateFlow<UpdateUiState> = _state.asStateFlow()

    private val _modalOpen = MutableStateFlow(false)
    val modalOpen: StateFlow<Boolean> = _modalOpen.asStateFlow()

    private var checkJob: Job? = null
    private var downloadJob: Job? = null

    /** Auto-check on activity resume. No-op (cache hit) when called within 24 h. */
    fun onAppOpen() {
        val current = _state.value
        if (current is UpdateUiState.Checking
            || current is UpdateUiState.Downloading
            || current is UpdateUiState.Launching) return
        checkJob?.cancel()
        checkJob = viewModelScope.launch {
            runCheck(force = false, openModalOnDone = false)
        }
    }

    /** User tapped "Check for updates" in Settings. Always opens the modal. */
    fun onForceCheck() {
        checkJob?.cancel()
        _state.value = UpdateUiState.Checking(stableSnapshot())
        _modalOpen.value = true
        checkJob = viewModelScope.launch {
            runCheck(force = true, openModalOnDone = true)
        }
    }

    /** Activity is backgrounding — abort any in-flight check. */
    fun cancelInFlightCheck() {
        checkJob?.cancel()
    }

    /** User tapped Install in the modal. */
    fun onInstall() {
        val info = (_state.value as? UpdateUiState.Available)?.info ?: return
        downloadJob?.cancel()
        downloadJob = viewModelScope.launch {
            try {
                _state.value = UpdateUiState.Downloading(info, 0f, 0L, 0L)
                downloader.download(info.apkUrl, info.latestVersion).collect { event ->
                    when (event) {
                        is UpdateDownloader.DownloadProgress.Started -> { }
                        is UpdateDownloader.DownloadProgress.Progress -> {
                            val pct = if (event.total > 0)
                                event.bytesRead.toFloat() / event.total else 0f
                            _state.value = UpdateUiState.Downloading(
                                info, pct, event.bytesRead, event.total
                            )
                        }
                        is UpdateDownloader.DownloadProgress.Done -> {
                            _state.value = UpdateUiState.Launching(info)
                            val outcome = withContext(Dispatchers.Main) {
                                Installer.installApk(app, event.file)
                            }
                            handleInstallOutcome(outcome)
                        }
                        is UpdateDownloader.DownloadProgress.Failed -> {
                            _state.value = UpdateUiState.Error(event.reason)
                        }
                    }
                }
            } catch (e: CancellationException) {
                _state.value = UpdateUiState.Available(
                    info, dismissed = prefs.isUpdateVersionDismissed(info.latestVersion)
                )
                throw e
            } catch (e: Exception) {
                AppLog.log("UpdateDownload", "Install flow error: ${e.message}")
                _state.value = UpdateUiState.Error(e.message ?: "Update failed")
            }
        }
    }

    private suspend fun handleInstallOutcome(outcome: Installer.InstallStartOutcome) {
        when (outcome) {
            Installer.InstallStartOutcome.LAUNCHED,
            Installer.InstallStartOutcome.MISSING_PERMISSION -> {
                // System took over (or user is in Settings granting permission).
                // Hold "Opening installer…" briefly so the transition isn't
                // jarring, then step out of the way.
                delay(LAUNCHING_TO_IDLE_DELAY_MS)
                _state.value = UpdateUiState.Idle
                _modalOpen.value = false
            }
            Installer.InstallStartOutcome.FILE_GONE ->
                _state.value = UpdateUiState.Error("Downloaded file missing")
            Installer.InstallStartOutcome.ERROR ->
                _state.value = UpdateUiState.Error("Could not start installer")
        }
    }

    /** User tapped "Skip this version". Persist dismissal; modal stays open. */
    fun onSkipVersion() {
        val info = (_state.value as? UpdateUiState.Available)?.info ?: return
        prefs.dismissUpdateVersion(info.latestVersion)
        _state.value = UpdateUiState.Available(info, dismissed = true)
    }

    /** User tapped the discovery banner. The modal opens against the
     *  existing Available state — no fresh network check. */
    fun openModal() {
        _modalOpen.value = true
    }

    /** User tapped Close on the modal. */
    fun onDismissModal() {
        _modalOpen.value = false
    }

    /** User tapped Cancel during a download. */
    fun onCancelDownload() {
        downloadJob?.cancel()
    }

    private suspend fun runCheck(force: Boolean, openModalOnDone: Boolean) {
        try {
            val info = checker.check(force = force)
            _state.value = when {
                info == null -> UpdateUiState.NoUpdate(currentVersion)
                info.isNewer -> {
                    prefs.cachedLatestVersion = info.latestVersion
                    UpdateUiState.Available(
                        info,
                        dismissed = prefs.isUpdateVersionDismissed(info.latestVersion),
                    )
                }
                else -> {
                    prefs.cachedLatestVersion = null
                    UpdateUiState.NoUpdate(info.currentVersion)
                }
            }
            if (openModalOnDone) _modalOpen.value = true
            prefs.lastUpdateCheckAt = System.currentTimeMillis()
        } catch (e: CancellationException) {
            // Backgrounded mid-check — revert silently to whatever was visible.
            val current = _state.value
            if (current is UpdateUiState.Checking) {
                _state.value = current.previous
            }
            throw e
        } catch (e: Exception) {
            AppLog.log("UpdateDownload", "Check failed: ${e.message}")
            _state.value = UpdateUiState.Error(e.message ?: "Update check failed")
        }
    }

    /** Returns current state, unwrapping `Checking.previous` so a fresh check
     *  while another is in flight doesn't lose the original state. */
    private fun stableSnapshot(): UpdateUiState {
        val s = _state.value
        return if (s is UpdateUiState.Checking) s.previous else s
    }

    companion object {
        private const val LAUNCHING_TO_IDLE_DELAY_MS = 800L
    }
}
