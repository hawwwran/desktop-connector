#!/usr/bin/env bash
# AppRun — AppImage entrypoint for desktop-connector.
#
# Sets up the runtime environment so the bundled Python finds its
# stdlib + site-packages, plus (once P.2a lands) the bundled GTK4 /
# libadwaita / pixbuf loaders / GIO modules / gsettings schemas.
# Then execs the bundled `python3.11 -m src.main` with whatever args
# the AppImage was invoked with.
#
# Layout follows niess/python-appimage convention: Python lives at
# $APPDIR/opt/python3.11/, with `usr/bin/python3.11` symlinking into it.
# Subprocess windows re-enter the same AppImage via $APPIMAGE (P.2b).
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
# no-op until P.2a actually bundles GTK4 + libadwaita; the directories
# simply don't exist yet, so prepending them is harmless.
export LD_LIBRARY_PATH="$APPDIR/usr/lib:$APPDIR/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$APPDIR/usr/lib/girepository-1.0:$APPDIR/usr/lib/x86_64-linux-gnu/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export GSETTINGS_SCHEMA_DIR="$APPDIR/usr/share/glib-2.0/schemas${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"
export GDK_PIXBUF_MODULE_FILE="$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
export XDG_DATA_DIRS="$APPDIR/usr/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}:/usr/local/share:/usr/share"

PYTHON_BIN="$PYTHONHOME/bin/python3.11"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "AppRun: bundled Python not found at $PYTHON_BIN" >&2
  exit 127
fi

cd "$APPDIR/usr/lib/desktop-connector" 2>/dev/null || true
exec "$PYTHON_BIN" -m src.main "$@"
