#!/usr/bin/env bash
# AppRun — AppImage entrypoint for desktop-connector.
#
# Sets up the runtime environment so the bundled Python finds the bundled
# GTK4 / libadwaita / typelibs / pixbuf loaders / gsettings schemas, then
# execs `python3 -m src.main` with whatever args the AppImage was invoked
# with. Subprocess windows re-enter the same AppImage via $APPIMAGE
# (P.2b), so this script is the single env-setup chokepoint.
#
# Filled in for real once the AppDir is being staged (P.1b/P.2a). Until
# then it works for a host-Python sanity check.
set -euo pipefail

HERE="$(dirname -- "$(readlink -f -- "$0")")"
APPDIR="${APPDIR:-$HERE}"
export APPDIR

# Bundled GTK / GI / pixbuf / GIO / schemas — paths relative to AppDir.
# Each prepends so any host value is preserved as fallback.
export LD_LIBRARY_PATH="$APPDIR/usr/lib:$APPDIR/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$APPDIR/usr/lib/girepository-1.0:$APPDIR/usr/lib/x86_64-linux-gnu/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export GSETTINGS_SCHEMA_DIR="$APPDIR/usr/share/glib-2.0/schemas${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"
export GDK_PIXBUF_MODULE_FILE="$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
export XDG_DATA_DIRS="$APPDIR/usr/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}:/usr/local/share:/usr/share"

# Bundled Python (linuxdeploy-plugin-python lands it at usr/bin/python3).
# PYTHONHOME points at the bundled prefix so site-packages resolves there.
# PYTHONPATH lets `python3 -m src.main` find the app source under usr/lib.
export PYTHONHOME="$APPDIR/usr"
export PYTHONPATH="$APPDIR/usr/lib/desktop-connector${PYTHONPATH:+:$PYTHONPATH}"

PYTHON_BIN="$APPDIR/usr/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
  # P.1a stub: bundled Python not present yet. Fall back to host python3
  # so the AppRun script is testable in isolation. P.1b removes this
  # branch (bundled Python becomes mandatory).
  PYTHON_BIN="$(command -v python3)"
  unset PYTHONHOME
fi

cd "$APPDIR/usr/lib/desktop-connector" 2>/dev/null || true
exec "$PYTHON_BIN" -m src.main "$@"
