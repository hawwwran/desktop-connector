# Brand rollout

Apply the visual identity from `docs/visual-identity-guide.md` across all three components. Android landed in v0.2.0 (2026-04-19). Desktop and server completed on 2026-04-30 on the `branding-plan` branch.

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

## Desktop — DONE

Landed across the same six-phase plan as Android, all in `desktop/src/brand.py`:

1. **Color tokens** — `brand.py` declares the palette as both hex strings (CSS / Tk) and RGB tuples (PIL compositing for the tray). `APP_ID`, `APP_NAME`, `ICON_NAME` constants give a single source of truth for compositor identity.
2. **GTK4 CSS** — `apply_brand_css()` installs a Gtk.CssProvider at `STYLE_PROVIDER_PRIORITY_USER` (above libadwaita's compiled theme). Re-defines accent + destructive named colours, plus explicit selector rules for buttons / switches / scales because libadwaita 1.5 bakes the accent gradient into its bundled theme at SCSS build time. Opt-in `.brand-action-accent` / `.brand-action-destructive` / `.brand-icon-destructive` classes for icon-only buttons that need brand colour. `apply_pointer_cursors()` walks the widget tree and sets the pointer cursor on every Gtk.Button / Gtk.Switch / Gtk.LinkButton (GTK4 has no CSS `cursor` property). Every `show_*` window in `windows.py` calls `apply_brand_css()` then `apply_theme_mode_from_config_dir(config_dir)` in its `on_activate`.
3. **Tray sparkle** — `tray.py` composites runtime icons from `desktop/assets/brand/star-{full,center}-bw.png`: shape is always a filled 4-point sparkle, colour carries state (connected=DcBlue800, remote_offline=DcBlue400, reconnecting=DcYellow500, disconnected=DcOrange700, uploading=full-yellow with optional yellow center diamond). Falls back to a flat-disc icon if the brand assets aren't shipped.
4. **App icon in install scripts** — `desktop/assets/brand/desktop-connector-{48,64,128,256}.png` ship in the dev tree and AppImage. `install-from-source.sh` copies them into `~/.local/share/icons/hicolor/{size}x{size}/apps/desktop-connector.png` and runs `gtk-update-icon-cache`. `appimage_install_hook.py` writes `Icon=desktop-connector` into the .desktop, autostart, and Nautilus / Nemo / Dolphin entries; AppImage payload bundles the same hicolor PNGs. `claim_gtk_identity()` sets `WM_CLASS` + `Adw.Application` `application_id` so the compositor groups every GTK4 subprocess into one branded taskbar tile.
5. **Theme pref** — `Config.theme_mode` (system/light/dark, default system) persists in `config.json`. `apply_theme_mode()` routes the value through `Adw.StyleManager.set_color_scheme(...)`. Settings window has an Appearance group with a `Adw.ComboRow` that live-applies on change. Mirrors the Android `ThemeMode` pref semantically. Test coverage: `tests/protocol/test_desktop_theme_mode_config.py`.
6. **Tk pairing window** — `brand_tk_window(root)` in `brand.py` sets the WM_CLASS + window icon. Pairing window calls it from its constructor; Tk's per-widget colour ceiling means the QR canvas + verification code still pick brand colours via Python literals rather than a stylesheet.

## Server — DONE

Server surfaces, status:

1. **Dashboard** (`server/src/Controllers/DashboardController.php` → HTML). Brand styling extracted into `server/public/css/brand.css` with named CSS variables (`--dc-blue-*`, `--dc-yellow-*`, `--dc-orange-*`, `--dc-white-soft`); dashboard links the stylesheet rather than embedding `<style>`. Inline `style="color:#hex"` patterns on status dots remain (one-shot per row, no cascade benefit) but can adopt the `.dc-status-*` utility classes from the stylesheet on next pass. Outer `server/.htaccess` got a `RewriteRule ^css/(.+\.css)$ public/css/$1 [L]` line so shared-host Apache deploys serve the stylesheet directly without routing through `index.php`.
2. **Pairing landing pages** — none exist. The pairing flow is JSON-only (`/api/pairing/request`/`/poll`/`/confirm`); QR rendering is client-side. Skipping.
3. **Error envelopes** — JSON, no visual treatment, skipped.
4. **Favicon** — `server/public/favicon-{32,64}.png` ship; outer .htaccess routes `^favicon-(\d+)\.png$` to them so they resolve at `<base>/favicon-N.png` regardless of the deploy subdirectory.
5. **README banner** — `docs/assets/banner.png` is the README's lead image. GitHub social-preview cards pick it up automatically when the README's first image is the banner.

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
