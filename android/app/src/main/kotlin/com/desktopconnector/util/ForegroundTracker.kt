package com.desktopconnector.util

import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.ProcessLifecycleOwner

/**
 * Process-wide foreground/background flag, driven by `ProcessLifecycleOwner`.
 * Read from any thread (Volatile). Used by the auto-switch logic in PollService:
 * when a transfer arrives while the app is in background, switch the selected
 * pair to the sender so the user lands on the right history when they open the
 * app. While the app is foregrounded, never auto-switch — disruptive.
 */
object ForegroundTracker {
    @Volatile var isForeground: Boolean = false
        private set

    fun install() {
        ProcessLifecycleOwner.get().lifecycle.addObserver(
            LifecycleEventObserver { _, event ->
                when (event) {
                    Lifecycle.Event.ON_START -> isForeground = true
                    Lifecycle.Event.ON_STOP -> isForeground = false
                    else -> Unit
                }
            }
        )
    }
}
