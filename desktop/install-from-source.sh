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
#   - You're on a distro the AppImage doesn't support (we'll add it
#     once we know about it).
#   - You're auditing the install steps and want them visible.
#
# Bouncing between this and the AppImage installer is safe (last
# install wins for system integration; ~/.config/desktop-connector/
# is shared and preserved). See
# `docs/plans/desktop-appimage-packaging-plan.md` for the model.
#
# Idempotent: safe to run multiple times (install, update, repair).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install-from-source.sh | bash
#   curl -fsSL ... | bash -s -- --version=0.1.1

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

PY_PKGS="pystray qrcode PyNaCl cryptography requests Pillow"
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

# --- File manager "Send to Phone" integration ---

FM_INSTALLED=false

# Nautilus (GNOME, Ubuntu, Zorin)
if command -v nautilus >/dev/null 2>&1; then
    NAUTILUS_SCRIPTS="$HOME/.local/share/nautilus/scripts"
    mkdir -p "$NAUTILUS_SCRIPTS"
    cp "$INSTALL_DIR/nautilus-send-to-phone.py" "$NAUTILUS_SCRIPTS/Send to Phone"
    chmod +x "$NAUTILUS_SCRIPTS/Send to Phone"
    info "Nautilus: Scripts → 'Send to Phone' installed"
    FM_INSTALLED=true
fi

# Nemo (Cinnamon, Mint)
if command -v nemo >/dev/null 2>&1; then
    NEMO_SCRIPTS="$HOME/.local/share/nemo/scripts"
    mkdir -p "$NEMO_SCRIPTS"
    cp "$INSTALL_DIR/nautilus-send-to-phone.py" "$NEMO_SCRIPTS/Send to Phone"
    chmod +x "$NEMO_SCRIPTS/Send to Phone"
    info "Nemo: Scripts → 'Send to Phone' installed"
    FM_INSTALLED=true
fi

# Dolphin (KDE)
if command -v dolphin >/dev/null 2>&1; then
    DOLPHIN_SERVICES="$HOME/.local/share/kservices5/ServiceMenus"
    mkdir -p "$DOLPHIN_SERVICES"
    cat > "$DOLPHIN_SERVICES/desktop-connector-send.desktop" << DOLPHIN_EOF
[Desktop Entry]
Type=Service
ServiceTypes=KonqPopupMenu/Plugin
MimeType=application/octet-stream;
Actions=sendToPhone

[Desktop Action sendToPhone]
Name=Send to Phone
Icon=$APP_NAME
Exec=$BIN_DIR/$APP_NAME --headless --send=%f
DOLPHIN_EOF
    info "Dolphin: 'Send to Phone' service menu installed"
    FM_INSTALLED=true
fi

if [ "$FM_INSTALLED" = false ]; then
    warn "No supported file manager found (Nautilus, Nemo, Dolphin). Right-click integration skipped."
fi

# --- Autostart (optional, don't overwrite if user removed it) ---

if [ ! -f "$AUTOSTART_FILE" ] && [ ! -f "$HOME/.config/$APP_NAME/.no-autostart" ]; then
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
    info "Autostart enabled (remove $AUTOSTART_FILE to disable)"
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

# Kill any existing instance
pkill -f "desktop-connector" 2>/dev/null || true
pkill -f "python3 -m src.main" 2>/dev/null || true
sleep 1

step "Starting Desktop Connector..."
nohup "$BIN_DIR/$APP_NAME" > /dev/null 2>&1 &
disown
info "Running in background (check system tray)"
