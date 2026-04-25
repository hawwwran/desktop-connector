#!/usr/bin/env bash
# Desktop Connector — uninstaller.
#
# Removes the AppImage at the canonical location, the .desktop entry,
# the autostart entry, the file-manager "Send to Phone" scripts, and
# any leftover bits from a prior install-from-source run
# (~/.local/bin/desktop-connector wrapper, ~/.local/share/desktop-connector/src/).
# Optionally removes ~/.config/desktop-connector/ (your pairings, keys,
# and history) — prompts before doing so.
#
# Run from either the repo (./desktop/uninstall.sh) or from the local
# copy install.sh dropped at $INSTALL_DIR/uninstall.sh.
#
# Usage:
#   ~/.local/share/desktop-connector/uninstall.sh
#   curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/uninstall.sh | bash
set -u

APP_NAME="desktop-connector"
INSTALL_DIR="$HOME/.local/share/$APP_NAME"
APPIMAGE_PATH="$INSTALL_DIR/$APP_NAME.AppImage"
LEGACY_BIN="$HOME/.local/bin/$APP_NAME"
CONFIG_DIR="$HOME/.config/$APP_NAME"
DESKTOP_FILE="$HOME/.local/share/applications/$APP_NAME.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/$APP_NAME.desktop"
NAUTILUS_SCRIPT="$HOME/.local/share/nautilus/scripts/Send to Phone"
NEMO_SCRIPT="$HOME/.local/share/nemo/scripts/Send to Phone"
DOLPHIN_SERVICE="$HOME/.local/share/kservices5/ServiceMenus/$APP_NAME-send.desktop"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }

# Stop any running Desktop Connector instance, identifying our processes
# specifically by APPIMAGE env var (catches the python child off the FUSE
# mount) or by CWD (catches legacy apt-pip launches). Avoids killing the
# user's own shell or unrelated dev-tree `python3 -m src.main` runs.
stop_existing_instance() {
    local target="$1"
    local install_dir
    install_dir=$(dirname "$target")
    local pid match cwd stopped=0
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
            kill -TERM "$pid" 2>/dev/null && stopped=$((stopped+1))
        fi
    done
    for pid in $(pgrep -f "$target" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        kill -TERM "$pid" 2>/dev/null && stopped=$((stopped+1))
    done
    if [ "$stopped" -gt 0 ]; then
        info "Stopped $stopped running instance(s)"
        sleep 2
    fi
}

echo
echo -e "${BOLD}Desktop Connector — uninstaller${NC}"
echo

stop_existing_instance "$APPIMAGE_PATH"

# If the user moved the AppImage elsewhere, the .desktop entry's Exec=
# points at the actual path. Read it before we delete the entry so we
# remove the right file.
if [ -f "$DESKTOP_FILE" ]; then
    EXEC_PATH=$(grep '^Exec=' "$DESKTOP_FILE" | head -n 1 | sed 's/^Exec=//' | awk '{print $1}')
    if [ -n "${EXEC_PATH:-}" ] && [ -f "$EXEC_PATH" ] && [ "$EXEC_PATH" != "$APPIMAGE_PATH" ]; then
        rm -f "$EXEC_PATH" && info "Removed AppImage: $EXEC_PATH"
    fi
fi

# Canonical-location AppImage.
if [ -f "$APPIMAGE_PATH" ]; then
    rm -f "$APPIMAGE_PATH" && info "Removed AppImage: $APPIMAGE_PATH"
fi

# Legacy install-from-source layout: src/ + .py files + uninstall.sh next
# to itself. Wipe the directory if it exists; the AppImage's own
# uninstall.sh sitting next to itself gets cleaned up implicitly by the
# rm -rf. We only blow away the directory we control, never $HOME.
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    info "Removed install dir: $INSTALL_DIR"
fi

# Legacy launcher wrapper from install-from-source.sh.
if [ -L "$LEGACY_BIN" ] || [ -f "$LEGACY_BIN" ]; then
    rm -f "$LEGACY_BIN"
    info "Removed launcher: $LEGACY_BIN"
fi

# Desktop integration files.
[ -f "$DESKTOP_FILE" ]   && rm -f "$DESKTOP_FILE"   && info "Removed app menu entry"
[ -f "$AUTOSTART_FILE" ] && rm -f "$AUTOSTART_FILE" && info "Removed autostart entry"

# File-manager scripts.
fm_removed=0
[ -e "$NAUTILUS_SCRIPT" ]  && rm -f "$NAUTILUS_SCRIPT"  && fm_removed=1
[ -e "$NEMO_SCRIPT" ]      && rm -f "$NEMO_SCRIPT"      && fm_removed=1
[ -e "$DOLPHIN_SERVICE" ]  && rm -f "$DOLPHIN_SERVICE"  && fm_removed=1
[ "$fm_removed" -eq 1 ] && info "Removed file-manager 'Send to Phone' scripts"

# Hicolor brand icons (legacy install-from-source path drops them here).
icon_removed=0
for size in 48 64 128 256; do
    f="$HOME/.local/share/icons/hicolor/${size}x${size}/apps/$APP_NAME.png"
    [ -f "$f" ] && rm -f "$f" && icon_removed=1
done
if [ "$icon_removed" -eq 1 ]; then
    command -v gtk-update-icon-cache >/dev/null && \
        gtk-update-icon-cache -q -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    info "Removed brand icons"
fi

# Cache (P.6 update-check + tray icon snapshots).
CACHE_DIR="$HOME/.cache/$APP_NAME"
if [ -d "$CACHE_DIR" ]; then
    rm -rf "$CACHE_DIR"
    info "Removed cache: $CACHE_DIR"
fi

# Config — prompt before nuking pairings + keys + history.
echo
if [ -d "$CONFIG_DIR" ]; then
    if [ -t 0 ]; then
        read -r -p "Remove config and keys ($CONFIG_DIR)? [y/N] " answer
    else
        # Non-interactive (curl | bash): keep config by default.
        answer="n"
        warn "Non-interactive run — keeping $CONFIG_DIR. Remove manually with: rm -rf $CONFIG_DIR"
    fi
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        rm -rf "$CONFIG_DIR"
        info "Removed config and keys"
    else
        info "Config kept at $CONFIG_DIR"
    fi
fi

echo
echo -e "${GREEN}${BOLD}Uninstalled.${NC}"
echo
