package com.desktopconnector.network

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Process-wide "server storage is full" flag.
 *
 * Set by UploadWorker when initTransfer returns 507 so a queued send
 * can't land; cleared when any subsequent init succeeds. The
 * HomeScreen banner + TransferViewModel observe this flow in the same
 * pattern as AuthObservation — visible, dismiss-on-success, no user
 * action required to clear.
 *
 * Kept outside ConnectionManager because UploadWorker is a short-lived
 * WorkManager process without a view-model scope, and we don't want
 * WorkManager to hold a long-lived reference to the ConnectionManager.
 */
object StoragePressure {
    private val _full = MutableStateFlow(false)
    val full: StateFlow<Boolean> = _full.asStateFlow()

    fun mark() { _full.value = true }
    fun clear() { _full.value = false }
}
