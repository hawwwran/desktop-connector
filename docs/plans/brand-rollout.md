# Brand rollout

Apply the visual identity from `docs/visual-identity-guide.md` across all three components. Android landed in v0.2.0 (2026-04-19). Desktop and server still pending.

## Android — DONE (v0.2.0)

Landed across 6 phases in one sitting:

1. **Color tokens** — `ui/theme/Color.kt` declares the brand palette (DcBlue970/950/900/800/700/500/400/200, DcYellow500/600, DcOrange700, DcWhiteSoft). `DcRed600` was defined and later retired when orange took over the `error` slot.
2. **Theme infrastructure** — `ui/theme/Theme.kt` ships dark + light `ColorScheme`s plus a `BrandColors` data class routed through `LocalBrandColors` `CompositionLocal` for connection/transfer semantics that don't map to Material3 slots. `ThemeMode` pref (`SYSTEM`/`LIGHT`/`DARK`, default `SYSTEM`) persists via `AppPreferences.themeMode` and flows through `MainActivity` / `ShareReceiverActivity` to `DesktopConnectorTheme(themeMode)`. Settings has a `SingleChoiceSegmentedButtonRow` selector wired for live recomposition.
3. **Status remap** — every hardcoded hex across `HomeScreen`, `StatusBar`, `PairingScreen`, `Navigation`, `SettingsScreen` replaced with brand tokens or Material3 slot references. Transfer row status colors, progress bars (upload=yellow, download=sky, delivering=strong light blue), connection dots, verification code, swipe-to-delete, FindPhone overlay. Settings card statuses (Long Polling, FCM, GPS, Battery, Paired device Online/Offline) wired to success (`onSurfaceVariant`) vs fail (`error`). All-icon-same-color pass in history rows (link, clipboard, APK, share, text → `onSurfaceVariant`).
4. **Notification stars** — `ic_notif_connected.xml` (filled 4-point sparkle) and `ic_notif_disconnected.xml` (outlined with `evenOdd` cutout) as hand-authored vectors from `docs/assets/star-full-bw.png` / `star-empty-bw.png`. `Notification.Builder.setColor(#2058F0)` on foreground-service, transfer-complete, and find-phone builders. Deleted 7 dead drawables (`ic_dot_*.xml`, `ic_notif_connecting`, `ic_notif_transfer`). Collateral fix: `PollService` now runs `api.healthCheck()` after silent null returns from `longPollNotify`/`getPendingTransfers` so airplane-mode disconnection actually flips `isConnected` and updates the notification (was previously stuck on "Connected").
5. **Launcher icon** — adaptive icon using `docs/assets/master-spark.png` as foreground (PNG per density mdpi..xxxhdpi), `@color/ic_launcher_background` = `#0920AC`, `ic_launcher_monochrome.xml` (safe-zone-compliant 4-point star) for Android 13+ themed icons. 15 dead raster mipmaps removed since minSdk=26.
6. **Splash screen** — `androidx.core:core-splashscreen:1.0.1`. `Theme.DesktopConnector.Splash` extends `Theme.SplashScreen` with `windowSplashScreenBackground` → `@color/splash_bg` (values/ = `#E8EEFD`, values-night/ = `#000733`) and `windowSplashScreenAnimatedIcon` → `@mipmap/ic_splash` (`master.png` at 288dp/density). Both launcher activities (`MainActivity`, `ShareReceiverActivity`) use the Splash theme in manifest and call `installSplashScreen()` before `super.onCreate()`. Bonus: `SideEffect` in `DesktopConnectorTheme` sets system bar colors + `isAppearanceLightStatusBars`/`NavigationBars` per theme.

**Decisions locked in along the way:**

- Success color is `DcBlue500` (not green, not orange — green folded into blue early, orange reassigned to danger/error later).
- Orange (`DcOrange700`) is the Material `error` slot — covers destructive (swipe-delete), failed transfers, FindPhone alarm overlay. Red is fully retired.
- Disconnected is muted blue (`DcBlue200` dark / `DcBlue400` light), NOT red — shape (notification star full/empty) carries the binary, color stays on-brand.
- In-app status indicators remain colored dots; stars are reserved for monochrome contexts (notifications). Shape-carries-state vs color-carries-state rule.
- Verification code uses yellow (`accentYellow` token) — one of the few places yellow appears in-app, matching the "spark" brand gesture.
- Settings action buttons are filled with `outline` bg + `DcBlue950` dark-blue text (no border). Unpair alone uses `error` (orange) bg + `DcBlue950` text; triggers a confirmation AlertDialog.

## Desktop — NOT STARTED

The desktop client (`desktop/src/`) has three visible surfaces that need the brand treatment:

1. **GTK4/libadwaita windows** (`windows.py`, subprocesses for settings/history/send-files/find-phone). Currently uses default Adwaita styling. Needs a CSS stylesheet wired via `Gtk.CssProvider` that maps brand colors to `@define-color` variables, plus an equivalent of the semantic status mapping table above. Light/dark should follow `Adw.StyleManager` color scheme or a per-config override matching the Android `ThemeMode` pref semantically.
2. **pystray tray icon** (`tray.py`). Currently a donut (colored ring + white center). Guide calls out the 4-point sparkle star as the brand mark. `docs/assets/star-center-bw.png` was authored specifically to support tray compositing: overlay different center-fill colors to signal state (connected=orange→now blue, disconnected=muted blue, reconnecting=yellow). PIL can composite the outer empty star + colored center disk at runtime.
3. **Tkinter pairing window** (`pairing.py`). Default Tk chrome. Tk's theming is limited; worst case is custom colors on labels/buttons without full theme support.

Suggested phasing (mirrors Android):
1. `desktop/src/brand.py` — color tokens matching Android `Color.kt`.
2. GTK4 CSS theme loaded on window creation.
3. Tray icon: composite star from `docs/assets/star-empty-bw.png` + center disk in brand color per state.
4. App icon in `install.sh` / `.desktop` files — point at a brand launcher PNG (reuse `docs/assets/master-spark.png` or adaptive Android foreground).
5. Config pref `theme_mode` in `config.py` mirroring Android; Settings window adds a dropdown.

## Server — NOT STARTED

Server surfaces that need the brand:

1. **Dashboard** (`server/src/Controllers/DashboardController.php` → HTML). Currently minimal inline CSS, not on-brand. Needs a shared CSS stylesheet with brand tokens.
2. **Pairing landing pages** (any HTML served by the relay during QR pairing flow, if applicable — check `server/public/`).
3. **Error envelopes** — JSON, no visual treatment, skip.
4. **Favicon** — server has no favicon currently. Use `docs/assets/master-spark.png` resized to 32/48 px.
5. **Project README** (`README.md`) and GitHub social preview — use `docs/assets/banner.png` for the social card.

Suggested phasing:
1. `server/public/css/brand.css` — palette + light/dark via `@media (prefers-color-scheme: dark)`.
2. Dashboard: apply brand CSS, restyle tables/cards/status badges using the same semantic mapping as Android.
3. Favicon from `master-spark.png`.
4. README header using `banner.png`.

## Assets authored

All in `docs/assets/`:

- `master.png` — full composed hero (devices + arc + spark + geometric background). Used as Android splash icon.
- `master-spark.png` — just the 4-point spark on the geometric bg, icon-friendly dense composition. Used as Android adaptive launcher foreground.
- `foreground.png` — devices + arc + spark on transparent. Alternative launcher foreground if the full composition is too dense at small sizes.
- `empty-master.png` — geometric bg only, no symbol. For hero plates / banners.
- `banner.png` — wide geometric plate. For README / social cards / server dashboard headers.
- `star-full-bw.png` — flat black 4-point sparkle, the brand's "connected/alive" mark.
- `star-empty-bw.png` — outline version of the star, "disconnected" mark.
- `star-center-bw.png` — just the inner diamond of the star; authored for desktop tray compositing (overlay colored fills per state).

## Not doing (decided against)

- Keeping red (`#DC2626`) in the palette — orange covers the error slot more on-brand. Material3 `error` = `DcOrange700`.
- Dynamic color / Material You wallpaper-sourced palette on Android — explicitly opted out so brand stays consistent across wallpapers.
- Stars for in-app status indicators — decided dots carry state via color in colored UI; stars only in monochrome (notifications).
- Legacy raster launcher icons on Android — deleted since minSdk=26 always resolves to adaptive.
