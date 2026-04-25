#!/usr/bin/env bash
# Desktop Connector — AppImage installer.
#
# Bootstraps a fresh install:
#   1. Fetch the public release-signing key from this repo.
#   2. Resolve the latest desktop/v* release on GitHub.
#   3. Download the AppImage + its detached .sig.
#   4. GPG-verify the signature against the public key.
#   5. Place the AppImage at ~/.local/share/desktop-connector/.
#   6. Launch it once. The AppImage's first-launch hook drops the
#      .desktop entry, autostart entry, and file-manager scripts; the
#      onboarding dialog asks for your relay server URL.
#
# Idempotent: re-running upgrades by replacing the AppImage and the
# in-app updater (P.6b) takes over future updates automatically.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash
#
# Trust model: install.sh and the public key both come from
# raw.githubusercontent.com/hawwwran/desktop-connector/main. If the
# repo is compromised, both are malicious — verify the public key
# fingerprint against multiple sources before trusting an installer
# (the recovery doc at docs/release/desktop-signing-recovery.md, the
# README, the project's social-media presence, etc.):
#
#   FBEFCEC1 3D7A EC08 1081 2975 491C 9043 90F4 E03B
#
# The signature itself protects against post-CI tampering of the
# AppImage on releases.github.com.
#
# To install from the source tree (contributors / dev work) instead of
# the AppImage, see install-from-source.sh in this folder.
set -euo pipefail

REPO="hawwwran/desktop-connector"
INSTALL_DIR="$HOME/.local/share/desktop-connector"
APPIMAGE_PATH="$INSTALL_DIR/desktop-connector.AppImage"
PUBKEY_URL="https://raw.githubusercontent.com/${REPO}/main/docs/release/desktop-signing.pub.asc"
EXPECTED_FP="FBEFCEC13D7AEC0810812975491C904390F4E03B"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
step()  { echo -e "${BOLD}[·]${NC} $1"; }
fail()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# Stop any running Desktop Connector instance that points at our canonical
# AppImage path or the canonical install dir. Matches the python child by
# its APPIMAGE env var (correct even when it's running off a FUSE mount at
# /tmp/.mount_*/), and the legacy apt-pip child by its CWD. Avoids the
# brittle `pkill -f 'python3 -m src.main'` pattern, which would also match
# unrelated dev-tree runs (and sometimes our own shell when the literal
# string appears in argv).
stop_existing_instance() {
    local target="$1"
    local install_dir
    install_dir=$(dirname "$target")
    local pid match cwd
    # Track killed PIDs to dedupe across the two passes — an AppImage
    # instance is two processes (runtime wrapper + python child) that
    # would otherwise both match and inflate the visible count to 2 for
    # what the user sees as one tray icon.
    declare -A killed=()
    for pid in $(pgrep -f 'python.*-m src\.main' 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        match=0
        if [ -r "/proc/$pid/environ" ] && \
           tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
             | grep -qx "APPIMAGE=$target"; then
            match=1
        else
            cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
            case "$cwd" in
                "$install_dir"|"$install_dir"/*) match=1 ;;
            esac
        fi
        if [ "$match" -eq 1 ]; then
            kill -TERM "$pid" 2>/dev/null && killed[$pid]=1
        fi
    done
    # AppImage runtime wrapper (matches by argv = canonical path).
    for pid in $(pgrep -f "$target" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        [ -n "${killed[$pid]:-}" ] && continue
        kill -TERM "$pid" 2>/dev/null && killed[$pid]=1
    done
    if [ "${#killed[@]}" -gt 0 ]; then
        info "Stopped existing Desktop Connector"
        sleep 2
    fi
}

# --- pre-flight ----------------------------------------------------------------

[ "$(id -u)" = "0" ] && fail "Don't run this as root — installs to your \$HOME."
command -v curl    >/dev/null || fail "curl is required. sudo apt install curl"
command -v gpg     >/dev/null || fail "gpg is required. sudo apt install gnupg"
command -v python3 >/dev/null || fail "python3 is required for parsing the GitHub Releases JSON."

echo
echo -e "${BOLD}Desktop Connector — AppImage installer${NC}"
echo

# --- resolve latest desktop/v* release -----------------------------------------

step "Resolving latest desktop release on GitHub..."
RELEASES_JSON=$(curl -fsSL \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/${REPO}/releases?per_page=30") \
    || fail "GitHub Releases API unreachable."

# Pick the most recent non-draft, non-prerelease desktop/v* release that
# has both an .AppImage asset AND a matching .AppImage.sig. python3 -c
# (not "python3 -") so stdin carries the JSON and the script lives in
# argv — they otherwise compete for /dev/stdin.
RESULT=$(printf '%s' "$RELEASES_JSON" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for r in data:
    if r.get("draft") or r.get("prerelease"):
        continue
    tag = r.get("tag_name", "")
    if not tag.startswith("desktop/v"):
        continue
    appimage_url = ""
    sig_url = ""
    for a in r.get("assets", []):
        n = a.get("name", "")
        if n.endswith(".AppImage"):
            appimage_url = a.get("browser_download_url", "")
        elif n.endswith(".AppImage.sig"):
            sig_url = a.get("browser_download_url", "")
    if appimage_url and sig_url:
        print(tag)
        print(appimage_url)
        print(sig_url)
        break
') || fail "Could not parse GitHub Releases response."

TAG=$(printf '%s' "$RESULT" | sed -n 1p)
APPIMAGE_URL=$(printf '%s' "$RESULT" | sed -n 2p)
SIG_URL=$(printf '%s' "$RESULT" | sed -n 3p)

[ -n "$TAG" ] && [ -n "$APPIMAGE_URL" ] && [ -n "$SIG_URL" ] \
    || fail "No signed desktop/v* release found on GitHub yet."
info "Found release: $TAG"

# --- tmp area ------------------------------------------------------------------

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

# --- public key ----------------------------------------------------------------

step "Fetching release-signing public key..."
curl -fsSL "$PUBKEY_URL" -o "$TMP/pubkey.asc" \
    || fail "Could not fetch $PUBKEY_URL"

GOT_FP=$(gpg --show-keys --with-colons "$TMP/pubkey.asc" 2>/dev/null \
    | awk -F: '/^fpr:/ {print $10; exit}')
[ "$GOT_FP" = "$EXPECTED_FP" ] \
    || fail "Public key fingerprint mismatch.\n    got:      $GOT_FP\n    expected: $EXPECTED_FP\n    Either the key was rotated or the repo has been tampered with — STOP and verify against docs/release/desktop-signing-recovery.md."
info "Public key fingerprint matches ($EXPECTED_FP)"

# Throwaway GNUPGHOME so we don't pollute the user's keyring with our key.
export GNUPGHOME="$TMP/gnupg"
mkdir -p "$GNUPGHOME"
chmod 700 "$GNUPGHOME"
gpg --batch --import "$TMP/pubkey.asc" >/dev/null 2>&1 \
    || fail "Could not import public key into $GNUPGHOME."

# --- download AppImage + signature --------------------------------------------

step "Downloading AppImage..."
curl -fSL --progress-bar "$APPIMAGE_URL" -o "$TMP/desktop-connector.AppImage" \
    || fail "AppImage download failed."

step "Downloading signature..."
curl -fsSL "$SIG_URL" -o "$TMP/desktop-connector.AppImage.sig" \
    || fail "Signature download failed."

# --- verify --------------------------------------------------------------------

step "Verifying signature..."
if ! gpg --batch --verify \
        "$TMP/desktop-connector.AppImage.sig" \
        "$TMP/desktop-connector.AppImage" 2> "$TMP/verify.log"; then
    cat "$TMP/verify.log"
    fail "Signature verification failed — refusing to install."
fi
info "Signature OK."

# --- place ---------------------------------------------------------------------

mkdir -p "$INSTALL_DIR"

# Replace any prior AppImage at the canonical path. Move-then-mark-executable
# is atomic from the launcher's perspective: even if a prior instance is
# running, its mmap'd file descriptor stays valid until that process exits.
mv -f "$TMP/desktop-connector.AppImage" "$APPIMAGE_PATH"
chmod +x "$APPIMAGE_PATH"
info "Installed AppImage: $APPIMAGE_PATH"

# Drop a local copy of uninstall.sh next to the AppImage so users don't need
# internet to remove the install.
step "Fetching uninstaller..."
if curl -fsSL "https://raw.githubusercontent.com/${REPO}/main/desktop/uninstall.sh" \
        -o "$INSTALL_DIR/uninstall.sh"; then
    chmod +x "$INSTALL_DIR/uninstall.sh"
    info "Installed uninstaller: $INSTALL_DIR/uninstall.sh"
else
    warn "Could not fetch uninstall.sh — to remove later, re-curl from this repo."
fi

# --- launch --------------------------------------------------------------------

# Stop any prior instance so the new AppImage's tray icon takes over the slot.
stop_existing_instance "$APPIMAGE_PATH"

echo
echo -e "${BOLD}Starting Desktop Connector...${NC}"
nohup "$APPIMAGE_PATH" > /dev/null 2>&1 &
disown
sleep 1
info "Running in background. Look for the tray icon in your panel."

echo
echo -e "${BOLD}First launch?${NC}"
echo "  - A welcome dialog will ask for your relay server URL."
echo "  - The AppImage drops its menu entry, autostart, and file-manager"
echo "    'Send to Phone' scripts on first run automatically."
echo "  - Future updates land via the in-app updater (tray menu →"
echo "    'Check for updates'); no need to re-run this installer."
echo
echo -e "${BOLD}Don't see a tray icon?${NC}"
echo "  GNOME needs the AppIndicator extension:"
echo "    sudo apt install gnome-shell-extension-appindicator"
echo "    (then enable it in Extensions and log out/in)"
echo "  Cinnamon, KDE, Mate, XFCE — should work out of the box."
echo
echo "Uninstall: $INSTALL_DIR/uninstall.sh"
echo
