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
