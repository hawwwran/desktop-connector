#!/bin/bash
set -e

# Desktop Connector — install from source tree (apt + pip).
#
# Most users want the AppImage installer at install.sh — it pulls a
# signed, self-contained binary from GitHub Releases, doesn't touch
# system Python, and self-updates via the in-app updater. This script
# is the **contributor / dev-tree path**: it copies the source tree
# from this repo into ~/.local/share/desktop-connector/, installs
# system + Python deps via apt + pip, and wires up
# ~/.local/bin/desktop-connector as a thin shell wrapper around
# `python3 -m src.main`.
#
# Use this if:
#   - You're hacking on the Python source and want changes to land
#     locally without rebuilding an AppImage every time.
#   - You're on a distro older than the AppImage's coverage floor
#     (which is glibc 2.39 / Zorin 17+, Mint 22+, Pop! 24.04+,
#     Ubuntu 24.04+, Debian 13+, Fedora 40+). Older Ubuntu LTSes
#     like 22.04 / Mint 21 / Zorin 16 — use this script. The
#     apt+pip path follows the host's distro versions and works
#     anywhere with python3 + GTK4-via-apt available.
#   - You're auditing the install steps and want them visible.
#
# Bouncing between this and the AppImage installer is safe (last
# install wins for system integration; ~/.config/desktop-connector/
# is shared and preserved). See desktop/packaging/appimage/README.md
# for the release packaging model.
#
# Idempotent: safe to run multiple times (install, update, repair).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install-from-source.sh | bash
#   curl -fsSL ... | bash -s -- --version=0.3.2

APP_NAME="desktop-connector"
INSTALL_VERSION=""

# Parse arguments
for arg in "$@"; do
    case "$arg" in
        --version=*) INSTALL_VERSION="${arg#--version=}" ;;
    esac
done
INSTALL_DIR="$HOME/.local/share/$APP_NAME"
BIN_DIR="$HOME/.local/bin"
DESKTOP_FILE="$HOME/.local/share/applications/$APP_NAME.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/$APP_NAME.desktop"
REPO_URL="https://github.com/hawwwran/desktop-connector"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
step()  { echo -e "${BOLD}[·]${NC} $1"; }
fail()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# Stop any running Desktop Connector — both shapes (AppImage child process
# off a /tmp/.mount_*/ FUSE mount, and source-tree python3 -m src.main from
# ~/.local/bin/desktop-connector). Matches AppImage instances by reading
# /proc/$pid/environ for APPIMAGE=$target, and source-tree instances by
# /proc/$pid/cwd inside the install dir. Avoids killing our own shell or
# unrelated dev-tree runs of `python3 -m src.main`.
stop_existing_instance() {
    local target="$1"
    local install_dir
    install_dir=$(dirname "$target")
    local pid match cwd
    # Dedupe PIDs across the two passes. An AppImage instance is two
    # processes (runtime wrapper + python child) that would otherwise
    # both match and inflate the count to 2 for one user-visible tray.
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
    for pid in $(pgrep -f "$target" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        [ -n "${killed[$pid]:-}" ] && continue
        kill -TERM "$pid" 2>/dev/null && killed[$pid]=1
    done
    if [ "${#killed[@]}" -gt 0 ]; then
        info "Stopped existing Desktop Connector"
        # Wait up to 3 s (30 × 100 ms) for the SIGTERM'd processes to exit.
        # Then SIGKILL anything still alive.
        local i
        for i in $(seq 1 30); do
            local still=0
            for pid in "${!killed[@]}"; do
                if kill -0 "$pid" 2>/dev/null; then still=1; break; fi
            done
            [ "$still" -eq 0 ] && break
            sleep 0.1
        done
        for pid in "${!killed[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                kill -KILL "$pid" 2>/dev/null && warn "SIGKILLed unresponsive pid $pid"
            fi
        done
    fi
}

# --- Checks ---

if [ "$(id -u)" = "0" ]; then
    fail "Do not run as root. The installer will use sudo when needed."
fi

echo ""
echo -e "${BOLD}Desktop Connector — Installer${NC}"
echo ""

# --- System packages ---

SYSTEM_PKGS="python3 python3-pip python3-tk python3-pil.imagetk python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-ayatanaappindicator3-0.1 xclip"
MISSING_PKGS=""

for pkg in $SYSTEM_PKGS; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        MISSING_PKGS="$MISSING_PKGS $pkg"
    fi
done

if [ -n "$MISSING_PKGS" ]; then
    step "Installing system packages:$MISSING_PKGS"
    sudo apt-get update -qq
    sudo apt-get install -y -qq $MISSING_PKGS
    info "System packages installed"
else
    info "System packages already installed"
fi

# --- Python packages ---

PY_PKGS="pystray qrcode PyNaCl cryptography requests Pillow keyring"
MISSING_PY=""

for pkg in $PY_PKGS; do
    if ! python3 -c "import importlib; importlib.import_module('${pkg%%[>=]*}')" >/dev/null 2>&1; then
        # Some package names differ from import names
        case $pkg in
            PyNaCl)      python3 -c "import nacl" 2>/dev/null || MISSING_PY="$MISSING_PY $pkg" ;;
            Pillow)      python3 -c "import PIL" 2>/dev/null || MISSING_PY="$MISSING_PY $pkg" ;;
            *)           MISSING_PY="$MISSING_PY $pkg" ;;
        esac
    fi
done

if [ -n "$MISSING_PY" ]; then
    step "Installing Python packages:$MISSING_PY"
    pip3 install --user --break-system-packages $MISSING_PY 2>/dev/null || \
    pip3 install --user $MISSING_PY
    info "Python packages installed"
else
    info "Python packages already installed"
fi

# --- Download / update app ---

mkdir -p "$INSTALL_DIR"

# Detect if running from inside the repo (local install) or via curl (remote install)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/src/main.py" ]; then
    step "Installing from local files..."
    cp -r "$SCRIPT_DIR/." "$INSTALL_DIR/"
    info "App files installed from local copy"
elif [ -n "$INSTALL_VERSION" ]; then
    step "Downloading version ${INSTALL_VERSION}..."
    TMP=$(mktemp -d)
    RELEASE_URL="$REPO_URL/releases/download/desktop/v${INSTALL_VERSION}/desktop-connector-${INSTALL_VERSION}.tar.gz"
    if curl -fsSL "$RELEASE_URL" | tar xz -C "$INSTALL_DIR"; then
        info "Installed v${INSTALL_VERSION} from release"
    else
        echo -e "${RED}Failed to download v${INSTALL_VERSION}. Check the version exists.${NC}"
        rm -rf "$TMP"
        exit 1
    fi
    rm -rf "$TMP"
else
    step "Downloading latest from main..."
    TMP=$(mktemp -d)
    if command -v git >/dev/null 2>&1; then
        git clone --quiet --depth 1 "$REPO_URL.git" "$TMP/repo" 2>/dev/null && \
            cp -r "$TMP/repo/desktop/." "$INSTALL_DIR/" || \
        { curl -fsSL "$REPO_URL/archive/refs/heads/main.tar.gz" | tar xz -C "$TMP" --strip-components=1 && \
          cp -r "$TMP/desktop/." "$INSTALL_DIR/"; }
    else
        curl -fsSL "$REPO_URL/archive/refs/heads/main.tar.gz" | tar xz -C "$TMP" --strip-components=1
        cp -r "$TMP/desktop/." "$INSTALL_DIR/"
    fi
    rm -rf "$TMP"
    info "App files installed from GitHub"
fi

# --- Brand icon (hicolor theme) ---

ICON_SRC="$INSTALL_DIR/assets/brand"
if [ -d "$ICON_SRC" ]; then
    for size in 48 64 128 256; do
        SRC="$ICON_SRC/desktop-connector-$size.png"
        DEST_DIR="$HOME/.local/share/icons/hicolor/${size}x${size}/apps"
        if [ -f "$SRC" ]; then
            mkdir -p "$DEST_DIR"
            cp -f "$SRC" "$DEST_DIR/$APP_NAME.png"
        fi
    done
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    fi
    info "Brand icon installed (hicolor)"
fi

# --- Create launcher ---

mkdir -p "$BIN_DIR"

cat > "$BIN_DIR/$APP_NAME" << 'EOF'
#!/bin/bash
cd "$HOME/.local/share/desktop-connector"
exec python3 -m src.main "$@"
EOF
chmod +x "$BIN_DIR/$APP_NAME"

# Ensure ~/.local/bin is on PATH
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    warn "$BIN_DIR is not on your PATH. Add this to your ~/.bashrc:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

info "Launcher installed: $BIN_DIR/$APP_NAME"

# --- Desktop entry (app menu) ---

mkdir -p "$(dirname "$DESKTOP_FILE")"

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=Desktop Connector
Comment=E2E encrypted file and clipboard sharing
Exec=$BIN_DIR/$APP_NAME
Icon=$APP_NAME
Terminal=false
Categories=Network;Utility;
StartupNotify=false
StartupWMClass=com.desktopconnector.Desktop
EOF

info "App menu entry installed"

# --- File manager "Send to <device>" integration ---
#
# File-manager send targets are now per-paired-device and managed by
# desktop/src/file_manager_integration.py. The sync runs at app
# startup, after pairing, after rename, and after unpair, so this
# script no longer drops a generic "Send to Phone" entry — the next
# launch will detect the file managers and write the right per-device
# scripts itself.

if command -v nautilus >/dev/null 2>&1 \
   || command -v nemo >/dev/null 2>&1 \
   || command -v dolphin >/dev/null 2>&1; then
    info "File-manager send targets will appear after pairing (Right-click → Scripts)"
else
    warn "No supported file manager found (Nautilus, Nemo, Dolphin). Right-click integration skipped."
fi

# --- Autostart (optional; last install wins unless user explicitly disabled it) ---

if [ ! -f "$HOME/.config/$APP_NAME/.no-autostart" ]; then
    mkdir -p "$(dirname "$AUTOSTART_FILE")"
    cat > "$AUTOSTART_FILE" << EOF
[Desktop Entry]
Type=Application
Name=Desktop Connector
Exec=$BIN_DIR/$APP_NAME
Icon=$APP_NAME
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
StartupWMClass=com.desktopconnector.Desktop
EOF
    info "Autostart entry installed/updated (remove $AUTOSTART_FILE to disable)"
else
    info "Autostart entry unchanged"
fi

# --- Server URL ---

CONFIG_DIR="$HOME/.config/$APP_NAME"
CONFIG_FILE="$CONFIG_DIR/config.json"
mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ] && grep -q "server_url" "$CONFIG_FILE"; then
    CURRENT_URL=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('server_url',''))" 2>/dev/null)
    info "Server URL already set: $CURRENT_URL"
else
    echo ""
    echo -e "${BOLD}Where is your relay server?${NC}"
    echo -e "  Example: https://example.com/SERVICES/desktop-connector"
    echo -e "  Press Enter to skip (you can set it later in Settings)"
    echo ""
    read -r -p "Server URL: " SERVER_URL
    if [ -n "$SERVER_URL" ]; then
        # Validate: must respond to /api/health
        SERVER_URL="${SERVER_URL%/}"
        step "Checking server..."
        if curl -fsSL --max-time 5 "$SERVER_URL/api/health" 2>/dev/null | grep -q '"ok"'; then
            info "Server is reachable"
        else
            warn "Server did not respond at $SERVER_URL/api/health"
            read -r -p "Save anyway? [y/N] " confirm
            if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                warn "Skipped — set the server URL in Settings after starting the app"
                SERVER_URL=""
            fi
        fi
    fi
    if [ -n "$SERVER_URL" ]; then
        if [ -f "$CONFIG_FILE" ]; then
            python3 -c "
import json
c = json.load(open('$CONFIG_FILE'))
c['server_url'] = '$SERVER_URL'
json.dump(c, open('$CONFIG_FILE', 'w'), indent=2)
"
        else
            echo "{\"server_url\": \"$SERVER_URL\"}" > "$CONFIG_FILE"
        fi
        info "Server URL set to: $SERVER_URL"
    else
        warn "Skipped — set the server URL in Settings after starting the app"
    fi
fi

# --- Done ---

echo ""
echo -e "${GREEN}${BOLD}Installation complete!${NC}"
echo ""
echo -e "  Starts automatically on login."
echo -e "  Uninstall:  ${BOLD}$INSTALL_DIR/uninstall.sh${NC}"
echo ""

# --- Clear Python cache ---

find "$INSTALL_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
find "$INSTALL_DIR" -name "*.pyc" -delete 2>/dev/null
info "Cleared Python cache"

# --- Start the app ---

# Stop any prior instance — both shapes. The "target" we pass is the
# canonical AppImage path (so an AppImage instance with APPIMAGE=that
# matches), but the helper's CWD-fallback also catches source-tree
# instances launched via ~/.local/bin/desktop-connector (whose CWD is
# under $INSTALL_DIR).
stop_existing_instance "$INSTALL_DIR/desktop-connector.AppImage"

step "Starting Desktop Connector..."
nohup "$BIN_DIR/$APP_NAME" > /dev/null 2>&1 &
disown
info "Running in background (check system tray)"
