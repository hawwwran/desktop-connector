#!/usr/bin/env bash
# build.sh — interactive AppImage build driver.
#
# Wraps build-appimage.sh with prompts for source choice (github vs.
# local), output dir, and confirmation. Persists answers at
# ~/.config/desktop-connector-build/state.json so the next run picks
# up where the previous left off.
#
# P.1a: stub. Only --help is implemented. The full prompt flow + state
# persistence + pre-flight checks land in P.1c per the UX spec in
# docs/plans/desktop-appimage-packaging-plan.md.
set -euo pipefail

PROG="$(basename -- "$0")"
SCRIPT_DIR="$(dirname -- "$(readlink -f -- "$0")")"

# Hardcoded — edit if the project ever moves repos. The user is never
# asked for a remote URL.
REMOTE_REPO="https://github.com/hawwwran/desktop-connector"

usage() {
  cat <<EOF
$PROG — interactive build driver for the desktop-connector AppImage.

USAGE
  $PROG                     Walk through prompts (source, output, confirm).
  $PROG --non-interactive   Run with last-saved state; fail if missing.
  $PROG --help              Print this message and exit.

WHAT IT DOES
  1. Pre-flight checks (vendored tools present or downloadable, disk
     space, host build deps, network reachability for github mode).
  2. Prompts for source: github (clone $REMOTE_REPO @ main) or local
     repo path. Defaults to last choice.
  3. Prompts for output directory. Defaults to last choice or \$PWD.
  4. Confirms before running, then invokes build-appimage.sh.
  5. On success, updates state and prints AppImage path + SHA-256.

STATE
  ~/.config/desktop-connector-build/state.json
    { "last_source": "github"|"local",
      "last_local_path": "/abs/path",
      "last_output_dir": "/abs/path" }

NOTES
  P.1a stub: prompts not yet implemented. Use build-appimage.sh
  directly for now. See docs/plans/desktop-appimage-packaging-plan.md.
EOF
}

if [[ $# -eq 0 ]]; then
  usage
  cat <<EOF >&2

$PROG: interactive prompt flow lands in P.1c. Until then, invoke:
  $SCRIPT_DIR/build-appimage.sh --source=<dir> --output=<dir>
EOF
  exit 0
fi

for arg in "$@"; do
  case "$arg" in
    --help|-h) usage; exit 0 ;;
    --non-interactive)
      echo "$PROG: --non-interactive accepted but not yet wired up (P.1c)." >&2
      exit 64
      ;;
    *)
      echo "$PROG: unknown argument: $arg" >&2
      echo "Try '$PROG --help' for usage." >&2
      exit 64
      ;;
  esac
done
