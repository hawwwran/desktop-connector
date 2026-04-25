#!/usr/bin/env bash
# build-appimage.sh — mechanical AppImage builder.
#
# Takes a source checkout + an output dir, produces
# desktop-connector-x86_64.AppImage in <output>/. No prompts, no
# state — wrapped by build.sh (P.1c) for interactive use.
#
# P.1a: stub. Only --help / arg parsing is implemented. The real build
# pipeline (linuxdeploy + plugin-python + plugin-gtk + appimagetool)
# lands in P.1b/P.2a per docs/plans/desktop-appimage-packaging-plan.md.
set -euo pipefail

PROG="$(basename -- "$0")"
SCRIPT_DIR="$(dirname -- "$(readlink -f -- "$0")")"

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
                   Default: unset (small drift between runs is OK in P.1a).

EXIT STATUS
  0  success.
  64 usage error (missing/invalid args).
  *  build failure (propagates from underlying tools).

NOTES
  P.1a stub: this script currently only parses args and prints what
  it would do. The actual bundling pipeline lands in P.1b.
  See docs/plans/desktop-appimage-packaging-plan.md.
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

echo "$PROG: P.1a stub — would build from '$SOURCE_DIR' into '$OUTPUT_DIR'."
echo "$PROG: bundling pipeline lands in P.1b."
exit 0
