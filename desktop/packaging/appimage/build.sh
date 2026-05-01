#!/usr/bin/env bash
# build.sh — interactive AppImage build driver.
#
# Wraps build-appimage.sh with prompts for source choice (github vs.
# local), output dir, and confirmation. Persists answers at
# ~/.config/desktop-connector-build/state.json so re-runs default to
# previous answers.
#
# Interactive wrapper around the mechanical AppImage builder.
set -euo pipefail

PROG="$(basename -- "$0")"
SCRIPT_DIR="$(dirname -- "$(readlink -f -- "$0")")"

# Hardcoded — edit if the project ever moves repos. The user is never
# asked for a remote URL.
REMOTE_REPO="https://github.com/hawwwran/desktop-connector"
STATE_DIR="$HOME/.config/desktop-connector-build"
STATE_FILE="$STATE_DIR/state.json"
TOOLS_DIR="$SCRIPT_DIR/.tools"
BUILDER="$SCRIPT_DIR/build-appimage.sh"
MIN_FREE_BYTES=$((2 * 1024 * 1024 * 1024))   # 2 GB pre-flight floor

# Tools the mechanical builder auto-downloads. Pre-flight checks
# whether they're already cached so the user knows the first run will
# fetch ~30 MB.
TOOLS_FILES=(
  "python3.11.14-cp311-cp311-manylinux2014_x86_64.AppImage"
  "appimagetool-x86_64.AppImage"
  "linuxdeploy-x86_64.AppImage"
  "linuxdeploy-plugin-gtk.sh"
  "appimageupdatetool-x86_64.AppImage"
)

NON_INTERACTIVE=0
TMP_DIRS=()

# trap cleans tmp clones from github mode on any exit path.
cleanup() {
  local rc=$?
  if [[ ${#TMP_DIRS[@]} -gt 0 ]]; then
    rm -rf -- "${TMP_DIRS[@]}"
  fi
  return $rc
}
trap cleanup EXIT INT TERM

usage() {
  cat <<EOF
$PROG — interactive build driver for the desktop-connector AppImage.

USAGE
  $PROG                     Walk through prompts (source, output, confirm).
  $PROG --non-interactive   Run with last-saved state; fail if missing.
  $PROG --help              Print this message and exit.

WHAT IT DOES
  1. Pre-flight checks (vendored tools present or downloadable, disk
     space, host build deps).
  2. Prompts for source: github (clone $REMOTE_REPO @ main) or local
     repo path. Defaults to last choice.
  3. Prompts for output directory. Defaults to last choice or \$PWD.
  4. Confirms before running, then invokes build-appimage.sh.
  5. On success, updates state and prints AppImage path + SHA-256.

STATE
  $STATE_FILE
    { "last_source": "github"|"local",
      "last_local_path": "/abs/path",
      "last_output_dir": "/abs/path" }
EOF
}

# --- arg parsing -----------------------------------------------------

for arg in "$@"; do
  case "$arg" in
    --help|-h) usage; exit 0 ;;
    --non-interactive) NON_INTERACTIVE=1 ;;
    *)
      echo "$PROG: unknown argument: $arg" >&2
      echo "Try '$PROG --help' for usage." >&2
      exit 64
      ;;
  esac
done

# --- state helpers ---------------------------------------------------

state_read() {
  # state_read <field>  — prints value or empty string. Never errors.
  local field="$1"
  [[ -f "$STATE_FILE" ]] || return 0
  python3 - "$STATE_FILE" "$field" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    print(data.get(sys.argv[2], "") or "")
except Exception:
    pass
PY
}

state_write() {
  # state_write <source> <local_path> <output_dir>
  mkdir -p -- "$STATE_DIR"
  python3 - "$STATE_FILE" "$1" "$2" "$3" <<'PY'
import json, sys
path, src, lp, out = sys.argv[1:5]
with open(path, "w") as f:
    json.dump({
        "last_source": src,
        "last_local_path": lp,
        "last_output_dir": out,
    }, f, indent=2)
PY
}

# --- prompt helper ---------------------------------------------------

# prompt <message> <default>  — prints message, returns user input or
# default. In non-interactive mode, returns default; if default is
# empty, fails loud with a useful error.
prompt() {
  local message="$1" default="${2-}"
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    if [[ -z "$default" ]]; then
      echo "$PROG: --non-interactive but no saved state for '$message'." >&2
      echo "Run interactively once first to populate $STATE_FILE." >&2
      exit 1
    fi
    printf '%s\n' "$default"
    return 0
  fi
  local reply=""
  read -r -p "$message" reply </dev/tty
  if [[ -z "$reply" ]]; then
    printf '%s\n' "$default"
  else
    printf '%s\n' "$reply"
  fi
}

# yn <message> <default-y-or-n>
yn() {
  local message="$1" default="$2"
  local reply
  reply="$(prompt "$message " "$default")"
  case "${reply,,}" in
    y|yes) return 0 ;;
    n|no)  return 1 ;;
    *)     [[ "${default,,}" == "y" ]] ;;
  esac
}

# --- pre-flight ------------------------------------------------------

preflight() {
  # Builder script
  if [[ ! -x "$BUILDER" ]]; then
    echo "$PROG: builder not found or not executable: $BUILDER" >&2
    exit 1
  fi

  # python3 (used for state + version.json read in builder)
  if ! command -v python3 >/dev/null; then
    echo "$PROG: python3 not on PATH (required for state + version parsing)." >&2
    exit 1
  fi

  # git (only required for github mode — checked lazily there too).
  # We warn here because preflight is the place to surface it.
  if ! command -v git >/dev/null; then
    echo "$PROG: warning — git not on PATH; github source mode will be unavailable." >&2
  fi

  # Disk space in $TMPDIR (used by mktemp + builder's WORK_DIR).
  local tmp="${TMPDIR:-/tmp}"
  local avail
  avail="$(df -B1 --output=avail "$tmp" 2>/dev/null | tail -1 | tr -d '[:space:]')"
  if [[ -n "$avail" && "$avail" -lt "$MIN_FREE_BYTES" ]]; then
    echo "$PROG: only $((avail / 1024 / 1024)) MiB free in $tmp; need at least 2 GiB." >&2
    exit 1
  fi

  # Vendored tools — auto-download is the builder's job; here we just
  # tell the user what'll happen.
  local missing=()
  for tool in "${TOOLS_FILES[@]}"; do
    [[ -e "$TOOLS_DIR/$tool" ]] || missing+=("$tool")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "$PROG: ${#missing[@]} vendored tool(s) missing in $TOOLS_DIR/."
    echo "       The builder will fetch them on first run (~30 MB)."
    if ! yn "Continue? [Y/n]:" "y"; then
      echo "$PROG: aborted by user." >&2
      exit 1
    fi
  fi
}

# --- source choice ---------------------------------------------------

# Prints chosen source kind to stdout: "github" or "local".
choose_source() {
  local last_source default reply
  last_source="$(state_read last_source)"
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    default="$last_source"
  else
    default="${last_source:-github}"
  fi
  while :; do
    reply="$(prompt "Build from: [g] github (pulls latest from main of $REMOTE_REPO) [l] local repo
Choice [g/l] (default: $default): " "$default")"
    case "${reply,,}" in
      g|github) printf 'github\n'; return 0 ;;
      l|local)  printf 'local\n';  return 0 ;;
      *)
        if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
          echo "$PROG: invalid saved source '$reply' in state file." >&2
          exit 1
        fi
        echo "  invalid — answer g or l." >&2
        ;;
    esac
  done
}

# Validates a local-source path and prints its absolute form. Re-prompts
# on failure (interactive); fails fast in non-interactive mode.
validate_local_path() {
  local last_path candidate abs remote
  last_path="$(state_read last_local_path)"
  while :; do
    candidate="$(prompt "Path to local desktop-connector repo
[${last_path:-(none — type a path)}]: " "$last_path")"
    if [[ -z "$candidate" ]]; then
      [[ "$NON_INTERACTIVE" -eq 1 ]] && {
        echo "$PROG: --non-interactive but no saved local path." >&2; exit 1; }
      echo "  type a path." >&2
      continue
    fi
    abs="$(eval echo "$candidate")"            # expand ~
    abs="$(readlink -f -- "$abs" 2>/dev/null || true)"
    if [[ -z "$abs" || ! -d "$abs" ]]; then
      [[ "$NON_INTERACTIVE" -eq 1 ]] && {
        echo "$PROG: saved local path does not exist: $candidate" >&2; exit 1; }
      echo "  not a directory: $candidate" >&2
      continue
    fi
    if [[ ! -f "$abs/version.json" || ! -d "$abs/desktop" ]]; then
      [[ "$NON_INTERACTIVE" -eq 1 ]] && {
        echo "$PROG: $abs is not a desktop-connector checkout." >&2; exit 1; }
      echo "  $abs has no version.json + desktop/ — pick a desktop-connector checkout." >&2
      continue
    fi
    # Optional remote check: warn if remote URL doesn't match the canonical one.
    if remote="$(git -C "$abs" remote get-url origin 2>/dev/null)"; then
      if [[ "$remote" != "$REMOTE_REPO" && "$remote" != "$REMOTE_REPO.git" ]]; then
        echo "  note: this repo's remote is $remote, not $REMOTE_REPO."
        if ! yn "Build anyway? [y/N]:" "n"; then
          continue
        fi
      fi
    fi
    printf '%s\n' "$abs"
    return 0
  done
}

# Shallow-clones the canonical repo into a tmp dir, registers it for
# cleanup, and prints the resolved path.
clone_github() {
  if ! command -v git >/dev/null; then
    echo "$PROG: git not installed; cannot use github source mode." >&2
    exit 1
  fi
  local sha clone_dir
  echo "Resolving HEAD on $REMOTE_REPO ..." >&2
  if ! sha="$(git ls-remote --quiet "$REMOTE_REPO" HEAD 2>/dev/null | awk '{print $1}')"; then
    echo "$PROG: git ls-remote failed (network? remote moved?)." >&2
    exit 1
  fi
  if [[ -z "$sha" ]]; then
    echo "$PROG: empty SHA from $REMOTE_REPO HEAD." >&2
    exit 1
  fi
  echo "  HEAD is $sha" >&2
  if ! yn "Pull latest from main of $REMOTE_REPO @ ${sha:0:12}? [Y/n]:" "y"; then
    return 2   # caller re-asks source choice
  fi
  clone_dir="$(mktemp -d -t desktop-connector-clone.XXXXXXXX)"
  TMP_DIRS+=("$clone_dir")
  echo "  shallow-cloning into $clone_dir ..." >&2
  if ! git clone --quiet --depth 1 --branch main "$REMOTE_REPO" "$clone_dir" 2>&1 | sed 's/^/    /'; then
    echo "$PROG: git clone failed." >&2
    exit 1
  fi
  printf '%s\n' "$clone_dir"
}

# --- output dir ------------------------------------------------------

prompt_output_dir() {
  local last_out default candidate abs
  last_out="$(state_read last_output_dir)"
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    default="$last_out"
  else
    default="${last_out:-$PWD}"
  fi
  while :; do
    candidate="$(prompt "Output directory [$default]: " "$default")"
    if [[ -z "$candidate" ]]; then
      [[ "$NON_INTERACTIVE" -eq 1 ]] && {
        echo "$PROG: --non-interactive but no saved output dir." >&2; exit 1; }
      echo "  type a path." >&2
      continue
    fi
    abs="$(eval echo "$candidate")"
    if ! mkdir -p -- "$abs" 2>/dev/null; then
      [[ "$NON_INTERACTIVE" -eq 1 ]] && {
        echo "$PROG: cannot create $abs" >&2; exit 1; }
      echo "  can't create: $abs" >&2
      continue
    fi
    abs="$(readlink -f -- "$abs")"
    if [[ ! -w "$abs" ]]; then
      [[ "$NON_INTERACTIVE" -eq 1 ]] && {
        echo "$PROG: $abs is not writable." >&2; exit 1; }
      echo "  not writable: $abs" >&2
      continue
    fi
    # Disk space check on chosen output dir
    local avail
    avail="$(df -B1 --output=avail "$abs" 2>/dev/null | tail -1 | tr -d '[:space:]')"
    if [[ -n "$avail" && "$avail" -lt "$MIN_FREE_BYTES" ]]; then
      [[ "$NON_INTERACTIVE" -eq 1 ]] && {
        echo "$PROG: only $((avail / 1024 / 1024)) MiB free at $abs." >&2; exit 1; }
      echo "  only $((avail / 1024 / 1024)) MiB free at $abs (need 2 GiB)." >&2
      continue
    fi
    printf '%s\n' "$abs"
    return 0
  done
}

# --- non-interactive state guard ------------------------------------

require_state_for_non_interactive() {
  [[ "$NON_INTERACTIVE" -eq 1 ]] || return 0
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "$PROG: --non-interactive but no state file at $STATE_FILE." >&2
    echo "Run interactively once first to record source + output dir." >&2
    exit 1
  fi
  local src lp od
  src="$(state_read last_source)"
  lp="$(state_read last_local_path)"
  od="$(state_read last_output_dir)"
  if [[ -z "$src" || -z "$od" ]]; then
    echo "$PROG: --non-interactive but state file at $STATE_FILE is incomplete." >&2
    echo "Need both last_source and last_output_dir; run interactively once." >&2
    exit 1
  fi
  if [[ "$src" == "local" && -z "$lp" ]]; then
    echo "$PROG: --non-interactive with last_source=local but no last_local_path." >&2
    exit 1
  fi
  if [[ "$src" != "github" && "$src" != "local" ]]; then
    echo "$PROG: --non-interactive but last_source='$src' is not 'github' or 'local'." >&2
    exit 1
  fi
}

# --- main flow -------------------------------------------------------

main() {
  require_state_for_non_interactive
  preflight

  local source_kind source_dir source_label
  while :; do
    source_kind="$(choose_source)"
    case "$source_kind" in
      local)
        source_dir="$(validate_local_path)"
        source_label="local: $source_dir"
        break
        ;;
      github)
        if source_dir="$(clone_github)"; then
          source_label="github @ $(git -C "$source_dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
          break
        fi
        # rc=2 means user declined the github confirm; ask again.
        ;;
    esac
  done

  local output_dir
  output_dir="$(prompt_output_dir)"

  cat <<EOF

About to build:
  source:  $source_label
  output:  $output_dir
  tools:   $TOOLS_DIR/

EOF
  if ! yn "Proceed? [Y/n]:" "y"; then
    echo "$PROG: aborted by user." >&2
    exit 1
  fi

  echo ""
  echo "=== running build-appimage.sh ==="
  if ! "$BUILDER" --source="$source_dir" --output="$output_dir"; then
    local rc=$?
    echo ""
    echo "$PROG: build failed (exit $rc). State file not updated." >&2
    exit "$rc"
  fi

  # Persist successful answers. For github source we don't have a
  # last_local_path to save; reuse whatever was already there.
  local saved_local_path
  saved_local_path="$(state_read last_local_path)"
  if [[ "$source_kind" == "local" ]]; then
    saved_local_path="$source_dir"
  fi
  state_write "$source_kind" "$saved_local_path" "$output_dir"

  # Surface the produced artefact.
  local apk
  apk="$(ls -1 "$output_dir"/desktop-connector-*-x86_64.AppImage 2>/dev/null | tail -1 || true)"
  if [[ -z "$apk" ]]; then
    apk="$(ls -1 "$output_dir"/*.AppImage 2>/dev/null | tail -1 || true)"
  fi
  echo ""
  if [[ -n "$apk" ]]; then
    local size sha
    size="$(stat -c %s -- "$apk")"
    sha="$(sha256sum -- "$apk" | awk '{print $1}')"
    echo "=== build OK ==="
    echo "  AppImage: $apk"
    echo "  size:     $((size / 1024 / 1024)) MiB ($size bytes)"
    echo "  sha256:   $sha"
  else
    echo "=== build OK but no AppImage found in $output_dir — check builder output. ==="
    exit 1
  fi
}

main "$@"
