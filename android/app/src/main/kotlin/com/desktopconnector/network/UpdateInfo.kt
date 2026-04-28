package com.desktopconnector.network

/**
 * Frozen snapshot of an update-check decision.
 *
 * `isNewer` is the raw release-vs-current comparison; the
 * dismissed-version filter is applied later in `UpdateViewModel` so
 * the same `UpdateInfo` can drive both the (banner-suppressed)
 * settings subtitle and the (banner-shown) main view.
 *
 * `stale = true` means the most recent network attempt failed and
 * we're replaying a cached release.
 */
data class UpdateInfo(
    val currentVersion: String,
    val latestVersion: String,
    val releaseUrl: String,
    val apkUrl: String,
    val releaseNotes: String,
    val isNewer: Boolean,
    val stale: Boolean,
)
