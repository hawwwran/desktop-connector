#!/usr/bin/env bash
# AppRun — AppImage entrypoint for desktop-connector.
#
# Sets up the runtime env so the bundled Python finds its stdlib +
# site-packages + GTK4 / libadwaita / pixbuf loaders / GIO modules /
# gsettings schemas, sources any apprun-hooks deposited by linuxdeploy
# plugins, then execs the bundled `python3.11 -m src.main` with
# whatever args the AppImage was invoked with.
#
# Layout follows niess/python-appimage convention: Python lives at
# $APPDIR/opt/python3.11/, with `usr/bin/python3.11` symlinking into it.
# GTK4 + libadwaita + GIO + typelibs live at $APPDIR/usr/ (P.2a).
#
# `--gtk-window=<NAME>` is a top-level dispatch that invokes
# `python -m src.windows <NAME>` instead of `src.main`. Used by the
# tray to spawn subprocess windows (P.2b) and as a smoke-test entry
# for verifying bundled GTK4 (P.2a).
set -euo pipefail

HERE="$(dirname -- "$(readlink -f -- "$0")")"
export APPDIR="${APPDIR:-$HERE}"

# Bundled Python (from niess/python-appimage). PYTHONHOME points at the
# Python prefix; the interpreter lives at $PYTHONHOME/bin/python3.11.
export PYTHONHOME="$APPDIR/opt/python3.11"
export PATH="$PYTHONHOME/bin:$PATH"

# App source. PYTHONPATH lets `python -m src.main` find our package.
export PYTHONPATH="$APPDIR/usr/lib/desktop-connector${PYTHONPATH:+:$PYTHONPATH}"

# GTK / GI / pixbuf / GIO / schemas — paths relative to AppDir. These
# only matter once P.2a has populated them via linuxdeploy-plugin-gtk.
export LD_LIBRARY_PATH="$APPDIR/usr/lib:$APPDIR/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$APPDIR/usr/lib/girepository-1.0:$APPDIR/usr/lib/x86_64-linux-gnu/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export GSETTINGS_SCHEMA_DIR="$APPDIR/usr/share/glib-2.0/schemas${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"
export GDK_PIXBUF_MODULE_FILE="$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
export XDG_DATA_DIRS="$APPDIR/usr/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}:/usr/local/share:/usr/share"

# WebKit looks for its helper processes (WebKitWebProcess, etc.) at
# WEBKIT_EXEC_PATH. Without this, find-phone's Leaflet WebView fails to
# load. Sandbox disabled because we only ever load our own bundled
# Leaflet+OSM map — no untrusted content goes through this WebView.
export WEBKIT_EXEC_PATH="$APPDIR/usr/lib/webkitgtk-6.0"
export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1

# Source apprun-hooks deposited by linuxdeploy-plugin-gtk (and friends).
# The plugin writes path-fixup snippets here that translate host-relative
# paths to AppDir-relative ones; without this GTK can't find its modules
# and pixbuf loaders.cache references the build-time host paths.
if [[ -d "$APPDIR/apprun-hooks" ]]; then
  for hook in "$APPDIR/apprun-hooks/"*.sh; do
    [[ -f "$hook" ]] && source "$hook"
  done
fi

# linuxdeploy-plugin-gtk's hook is conservative on theming: it forces
# GTK_THEME=Adwaita ("Custom themes are broken" — its words) and
# GDK_BACKEND=x11 regardless of session. That makes the AppImage look
# stark white-Adwaita on systems with Zorin / Yaru / Pop! GTK themes,
# and forces XWayland on Wayland sessions (worse fractional scaling +
# extra D-Bus hops). Override after the hook runs:
#
#   - Drop GTK_THEME so libadwaita/GTK4 read `gtk-theme` from
#     org.gnome.desktop.interface and find the matching CSS at
#     $XDG_DATA_DIRS/themes/<name>/gtk-4.0/gtk.css (system /usr/share
#     is in XDG_DATA_DIRS via the line above).
#   - Drop GDK_BACKEND so GTK4 picks Wayland on Wayland sessions, X11
#     elsewhere. Bundled GTK4 4.10+ + libadwaita 1.5+ are stable on
#     both backends; the hook's "crashes on Wayland" warning predates
#     that and applied to GTK3 + older builds.
#   - Extend GSETTINGS_SCHEMA_DIR to include system schema dirs. The
#     hook hard-pins it to $APPDIR-only; without system schemas the
#     dconf settings backend can't deserialise system-only settings
#     (custom Zorin/Yaru schemas, third-party apps' schemas).
unset GTK_THEME
unset GDK_BACKEND
export GSETTINGS_SCHEMA_DIR="$APPDIR/usr/share/glib-2.0/schemas:/usr/share/glib-2.0/schemas:/usr/local/share/glib-2.0/schemas${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"

PYTHON_BIN="$PYTHONHOME/bin/python3.11"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "AppRun: bundled Python not found at $PYTHON_BIN" >&2
  exit 127
fi

# Top-level dispatcher: --gtk-window=<NAME> launches one of the
# subprocess windows directly. Recognized names match `windows.py`:
# send-files, settings, history, pairing, find-phone.
if [[ "${1:-}" == --gtk-window=* ]]; then
  WINDOW_NAME="${1#--gtk-window=}"
  shift
  cd "$APPDIR/usr/lib/desktop-connector" 2>/dev/null || true
  exec "$PYTHON_BIN" -m src.windows "$WINDOW_NAME" "$@"
fi

cd "$APPDIR/usr/lib/desktop-connector" 2>/dev/null || true
exec "$PYTHON_BIN" -m src.main "$@"
