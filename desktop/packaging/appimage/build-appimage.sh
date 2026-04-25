#!/usr/bin/env bash
# build-appimage.sh — mechanical AppImage builder.
#
# Takes a source checkout + an output dir, produces
# desktop-connector-x86_64.AppImage in <output>/. No prompts, no
# state — wrapped by build.sh (P.1c) for interactive use.
#
# P.1b: minimal AppImage with bundled Python + pure-Python deps. No
# GTK4 / libadwaita yet (P.2a). Anything that imports `gi` (settings
# subprocess windows, libadwaita) will fail at runtime — expected.
#
# Approach: we use niess/python-appimage as the relocatable Python
# source instead of niess/linuxdeploy-plugin-python (the plugin is
# deprecated in favour of python-appimage). Same upstream wheels,
# same manylinux2014 base. Keeps the AppRun-relative layout stable.
set -euo pipefail

PROG="$(basename -- "$0")"
SCRIPT_DIR="$(dirname -- "$(readlink -f -- "$0")")"

# Pinned upstream URLs. Bump deliberately and update the AppImage SHA
# pinning in P.5b's reproducibility plan when these change.
PYTHON_APPIMAGE_URL="https://github.com/niess/python-appimage/releases/download/python3.11/python3.11.14-cp311-cp311-manylinux2014_x86_64.AppImage"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
LINUXDEPLOY_URL="https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage"
# linuxdeploy-plugin-gtk has no releases; sourced from the master branch.
# When upstream cuts releases (open issue), pin to a tag.
LINUXDEPLOY_PLUGIN_GTK_URL="https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh"

TOOLS_DIR="${TOOLS_DIR:-$SCRIPT_DIR/.tools}"

usage() {
  cat <<EOF
$PROG — build the desktop-connector AppImage.

USAGE
  $PROG --source=<dir> --output=<dir>
  $PROG --help

OPTIONS
  --source=<dir>   Path to desktop-connector checkout root (must contain
                   version.json and a desktop/ subdirectory).
  --output=<dir>   Directory to write the produced AppImage into. Created
                   if missing. Existing artefact at the same name is
                   overwritten (idempotent re-run).
  --help, -h       Print this message and exit.

ENVIRONMENT
  TOOLS_DIR        Override the vendored tools directory.
                   Default: $SCRIPT_DIR/.tools
  SOURCE_DATE_EPOCH
                   Pin SquashFS timestamps for reproducible builds.
                   Default: unset (small drift between runs is OK in P.1b).

EXIT STATUS
  0  success.
  64 usage error (missing/invalid args).
  *  build failure (propagates from underlying tools).

NOTES
  P.1b stage: ships Python + pure-Python deps + src/. GTK4 bundling
  arrives in P.2a. See docs/plans/desktop-appimage-packaging-plan.md.
EOF
}

SOURCE_DIR=""
OUTPUT_DIR=""

if [[ $# -eq 0 ]]; then
  usage
  exit 64
fi

for arg in "$@"; do
  case "$arg" in
    --help|-h) usage; exit 0 ;;
    --source=*) SOURCE_DIR="${arg#--source=}" ;;
    --output=*) OUTPUT_DIR="${arg#--output=}" ;;
    *)
      echo "$PROG: unknown argument: $arg" >&2
      echo "Try '$PROG --help' for usage." >&2
      exit 64
      ;;
  esac
done

if [[ -z "$SOURCE_DIR" || -z "$OUTPUT_DIR" ]]; then
  echo "$PROG: --source and --output are both required." >&2
  echo "Try '$PROG --help' for usage." >&2
  exit 64
fi

SOURCE_DIR="$(readlink -f -- "$SOURCE_DIR")"
mkdir -p -- "$OUTPUT_DIR"
OUTPUT_DIR="$(readlink -f -- "$OUTPUT_DIR")"

if [[ ! -f "$SOURCE_DIR/version.json" ]]; then
  echo "$PROG: $SOURCE_DIR is not a desktop-connector checkout (no version.json)" >&2
  exit 1
fi
if [[ ! -d "$SOURCE_DIR/desktop/src" ]]; then
  echo "$PROG: $SOURCE_DIR/desktop/src not found" >&2
  exit 1
fi

# Read app version from version.json (host python is fine for this).
APP_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["desktop"])' "$SOURCE_DIR/version.json")"
echo "$PROG: building desktop-connector $APP_VERSION"

# Tmp work area. Cleaned on every exit path.
WORK_DIR="$(mktemp -d -t desktop-connector-appimage.XXXXXXXX)"
trap 'rm -rf -- "$WORK_DIR"' EXIT INT TERM

APPDIR="$WORK_DIR/AppDir"
mkdir -p -- "$APPDIR"

mkdir -p -- "$TOOLS_DIR"

ensure_tool() {
  local name="$1" url="$2"
  local path="$TOOLS_DIR/$name"
  if [[ -x "$path" ]]; then
    return
  fi
  echo "$PROG: downloading $name ..."
  curl -fSL --retry 3 -o "$path" "$url"
  chmod +x -- "$path"
}

ensure_tool python-appimage.AppImage "$PYTHON_APPIMAGE_URL"
ensure_tool appimagetool-x86_64.AppImage "$APPIMAGETOOL_URL"
ensure_tool linuxdeploy-x86_64.AppImage "$LINUXDEPLOY_URL"
ensure_tool linuxdeploy-plugin-gtk.sh "$LINUXDEPLOY_PLUGIN_GTK_URL"

# Pre-flight: verify host has GTK4 + libadwaita 1.5+ for the plugin to
# pull from. The plugin uses pkg-config to find the source paths.
need_pc_min() {
  local pkg="$1" min="$2"
  if ! pkg-config --exists "$pkg"; then
    echo "$PROG: host pkg-config can't find $pkg — install dev headers." >&2
    return 1
  fi
  if ! pkg-config --atleast-version="$min" "$pkg"; then
    local got
    got="$(pkg-config --modversion "$pkg")"
    echo "$PROG: host $pkg is $got but $min+ is required for libadwaita 1.5+ paint paths." >&2
    echo "       on Ubuntu 22.04 enable the GNOME 46 PPA and re-run." >&2
    return 1
  fi
}
need_pc_min gtk4 4.10 || exit 1
need_pc_min libadwaita-1 1.5 || exit 1
need_pc_min girepository-2.0 2.80 || exit 1

# Extract python-appimage. Provides relocatable CPython under squashfs-root/.
echo "$PROG: extracting bundled Python ..."
EXTRACT_DIR="$WORK_DIR/extract"
mkdir -p -- "$EXTRACT_DIR"
(cd "$EXTRACT_DIR" && "$TOOLS_DIR/python-appimage.AppImage" --appimage-extract >/dev/null)

PY_APPDIR="$EXTRACT_DIR/squashfs-root"
if [[ ! -d "$PY_APPDIR/opt" || ! -d "$PY_APPDIR/usr" ]]; then
  echo "$PROG: unexpected python-appimage layout in $PY_APPDIR" >&2
  ls -la "$PY_APPDIR" >&2 || true
  exit 1
fi

# Lift python-appimage's opt/ + usr/ into our AppDir wholesale. This
# preserves RUNPATH=$ORIGIN/../lib relocation so the interpreter loads
# its own libpython3.11.so.* from the bundle.
cp -a "$PY_APPDIR/opt" "$APPDIR/"
cp -a "$PY_APPDIR/usr" "$APPDIR/"

PYTHON_BIN="$APPDIR/opt/python3.11/bin/python3.11"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "$PROG: bundled python3.11 not found at $PYTHON_BIN" >&2
  exit 1
fi

# pip-install the desktop's requirements into the bundled site-packages.
# --no-cache-dir keeps the build dir tidy. Pinned manylinux2014 wheels
# (PyNaCl, cryptography, Pillow) bring their own bundled .so files.
echo "$PROG: installing Python deps ..."
PIP_DISABLE_PIP_VERSION_CHECK=1 \
"$PYTHON_BIN" -m pip install --no-cache-dir --no-warn-script-location \
  -r "$SOURCE_DIR/desktop/requirements.txt"

# PyGObject + pycairo provide the Python bindings to GObject Introspection
# (the `gi` package). Not in requirements.txt because the apt-pip install
# gets them from `python3-gi` / `python3-cairo` system packages — adding
# them to requirements.txt would needlessly compile-from-source on hosts
# that already have the apt versions. AppImage installs explicitly here.
#
# Build-time deps (the recipe's pre-flight ensures these): GLib 2.80+
# brought girepository-2.0; PyGObject 3.50+ requires it. On Ubuntu 24.04
# / Zorin 18 install `libgirepository-2.0-dev` to get the .pc file.
echo "$PROG: installing GObject bindings ..."
PIP_DISABLE_PIP_VERSION_CHECK=1 \
"$PYTHON_BIN" -m pip install --no-cache-dir --no-warn-script-location \
  "PyGObject>=3.50.0" "pycairo>=1.26.0"

# Bundle GTK4 + libadwaita + GIO + pixbuf loaders + GI typelibs (P.2a)
# via linuxdeploy-plugin-gtk. The plugin needs LINUXDEPLOY pointing at
# an extracted linuxdeploy binary (not the AppImage) — otherwise its
# inner deploy calls require FUSE, which not every host has.
echo "$PROG: extracting linuxdeploy ..."
LD_EXTRACT="$WORK_DIR/linuxdeploy-extract"
mkdir -p -- "$LD_EXTRACT"
(cd "$LD_EXTRACT" && "$TOOLS_DIR/linuxdeploy-x86_64.AppImage" --appimage-extract >/dev/null)
LINUXDEPLOY_BIN="$LD_EXTRACT/squashfs-root/AppRun"
if [[ ! -x "$LINUXDEPLOY_BIN" ]]; then
  echo "$PROG: extracted linuxdeploy AppRun not executable at $LINUXDEPLOY_BIN" >&2
  exit 1
fi

echo "$PROG: bundling GTK4 + libadwaita ..."
LINUXDEPLOY="$LINUXDEPLOY_BIN" \
DEPLOY_GTK_VERSION=4 \
  bash "$TOOLS_DIR/linuxdeploy-plugin-gtk.sh" --appdir "$APPDIR"

# Force-deploy libs that gi.require_version() loads lazily — linuxdeploy
# only follows direct ELF deps, so anything Python pulls in via GI at
# runtime needs to be added by hand. Three groups:
#
#  - WebKit  (find-phone Leaflet WebView)
#  - GTK3    (pystray tray backend imports Gtk-3.0 internally)
#  - libayatana-appindicator3  (pystray's _appindicator backend)
#
# The typelibs themselves (Gtk-3.0.typelib, AyatanaAppIndicator3-0.1.typelib,
# WebKit-6.0.typelib) are already pulled in by linuxdeploy-plugin-gtk's
# typelib sweep; we only need to ensure the matching .so files arrive.
echo "$PROG: bundling WebKit + GTK3 + AppIndicator ..."
HOST_LIBDIR="/usr/lib/x86_64-linux-gnu"
ld_lib_args=()
for lib in \
  libwebkitgtk-6.0.so.4 libjavascriptcoregtk-6.0.so.1 \
  libgtk-3.so.0 libayatana-appindicator3.so.1 \
; do
  if [[ -f "$HOST_LIBDIR/$lib" ]]; then
    ld_lib_args+=( --library "$HOST_LIBDIR/$lib" )
  fi
done
if (( ${#ld_lib_args[@]} > 0 )); then
  "$LINUXDEPLOY_BIN" --appdir "$APPDIR" "${ld_lib_args[@]}"
fi

# Helper process binaries (WebKitWebProcess / WebKitNetworkProcess /
# WebKitGPUProcess + the injected bundle .so). WEBKIT_EXEC_PATH (set
# by AppRun) points WebKit at this dir.
WEBKIT_HELPER_SRC="$HOST_LIBDIR/webkitgtk-6.0"
WEBKIT_HELPER_DST="$APPDIR/usr/lib/webkitgtk-6.0"
if [[ -d "$WEBKIT_HELPER_SRC" ]]; then
  mkdir -p -- "$WEBKIT_HELPER_DST"
  cp -a "$WEBKIT_HELPER_SRC/"* "$WEBKIT_HELPER_DST/"
fi

# Typelibs read by `from gi.repository import WebKit`.
GIR_DST="$APPDIR/usr/lib/girepository-1.0"
mkdir -p -- "$GIR_DST"
for typelib in WebKit-6.0.typelib WebKitWebProcessExtension-6.0.typelib; do
  if [[ -f "$HOST_LIBDIR/girepository-1.0/$typelib" ]]; then
    cp "$HOST_LIBDIR/girepository-1.0/$typelib" "$GIR_DST/"
  fi
done

# Copy desktop source.
mkdir -p -- "$APPDIR/usr/lib/desktop-connector"
cp -a "$SOURCE_DIR/desktop/src" "$APPDIR/usr/lib/desktop-connector/"

# Embed version.json so --version (and runtime version checks) can find it.
mkdir -p -- "$APPDIR/usr/share/desktop-connector"
cp "$SOURCE_DIR/version.json" "$APPDIR/usr/share/desktop-connector/"

# Drop brand icons into hicolor.
for sz in 48 64 128 256; do
  src="$SOURCE_DIR/desktop/assets/brand/desktop-connector-${sz}.png"
  if [[ ! -f "$src" ]]; then
    echo "$PROG: missing brand icon: $src" >&2
    exit 1
  fi
  dst_dir="$APPDIR/usr/share/icons/hicolor/${sz}x${sz}/apps"
  mkdir -p -- "$dst_dir"
  cp "$src" "$dst_dir/desktop-connector.png"
done

# AppImage tooling needs a top-level icon + .DirIcon + .desktop.
cp "$SOURCE_DIR/desktop/assets/brand/desktop-connector-256.png" "$APPDIR/desktop-connector.png"
cp "$APPDIR/desktop-connector.png" "$APPDIR/.DirIcon"

# Replace python-appimage's AppRun + .desktop with ours.
rm -f -- "$APPDIR/AppRun" "$APPDIR/"*.desktop
cp "$SCRIPT_DIR/AppRun.sh" "$APPDIR/AppRun"
chmod +x -- "$APPDIR/AppRun"
cp "$SCRIPT_DIR/desktop-connector.desktop" "$APPDIR/desktop-connector.desktop"

# Pack with appimagetool. --no-appstream skips an optional metainfo
# validation we don't ship metainfo for yet (could revisit in P.7).
OUTPUT_PATH="$OUTPUT_DIR/desktop-connector-x86_64.AppImage"
rm -f -- "$OUTPUT_PATH"
echo "$PROG: packing AppImage ..."
ARCH=x86_64 "$TOOLS_DIR/appimagetool-x86_64.AppImage" --no-appstream "$APPDIR" "$OUTPUT_PATH"

sha256="$(sha256sum "$OUTPUT_PATH" | awk '{print $1}')"
size="$(du -h "$OUTPUT_PATH" | awk '{print $1}')"
echo
echo "=== built ==="
echo "  path:    $OUTPUT_PATH"
echo "  version: $APP_VERSION"
echo "  size:    $size"
echo "  sha256:  $sha256"
