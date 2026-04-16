# Desktop Connector — Android

Android client for [Desktop Connector](../README.md). Kotlin + Jetpack Compose app with a foreground service for receiving transfers and WorkManager for background uploads.

## Install

Download the APK from [Releases](../../releases) and install it. The app registers as a system-wide "Share to" target — send files from any app directly to your desktop.

## Build

```bash
export ANDROID_HOME=/opt/android-sdk
cd android && ./gradlew assembleDebug
# APK at: app/build/outputs/apk/debug/app-debug.apk
```

## Features

- QR code pairing (scan from desktop app)
- Send files and clipboard (text/images) to desktop
- Share intent — share from any Android app (gallery, browser, file manager)
- Recent files strip for quick sending
- Transfer history with delivery status and swipe-to-delete
- Smart link detection — shared URLs show a link icon, tap to open or copy
- APK install — tap received APK in history to install
- Near-instant delivery via long polling (~1s latency)
- FCM push wake for background delivery (when server has Firebase configured)
- Zero battery drain — no polling when screen is off
- Pull-to-refresh resets connection backoff
- Dark Material3 theme

## Architecture

- **Crypto**: X25519 key exchange (Bouncy Castle) + AES-256-GCM + HKDF-SHA256
- **Networking**: OkHttp with exponential backoff connection manager
- **Background**: Foreground service for polling, WorkManager for uploads
- **Storage**: Room database for transfer queue, EncryptedSharedPreferences for keys
- **FCM**: Dynamic Firebase initialization from server config (no baked-in `google-services.json`)
