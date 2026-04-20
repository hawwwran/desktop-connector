#!/bin/bash

# Desktop Connector — Uninstaller

APP_NAME="desktop-connector"
INSTALL_DIR="$HOME/.local/share/$APP_NAME"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/$APP_NAME"
DESKTOP_FILE="$HOME/.local/share/applications/$APP_NAME.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/$APP_NAME.desktop"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}Desktop Connector — Uninstaller${NC}"
echo ""

rm -f "$BIN_DIR/$APP_NAME"
echo -e "${GREEN}[✓]${NC} Removed launcher"

rm -f "$DESKTOP_FILE"
echo -e "${GREEN}[✓]${NC} Removed app menu entry"

rm -f "$AUTOSTART_FILE"
echo -e "${GREEN}[✓]${NC} Removed autostart entry"

rm -f "$HOME/.local/share/nautilus/scripts/Send to Phone"
rm -f "$HOME/.local/share/nemo/scripts/Send to Phone"
rm -f "$HOME/.local/share/kservices5/ServiceMenus/desktop-connector-send.desktop"
echo -e "${GREEN}[✓]${NC} Removed file manager integrations"

for size in 48 64 128 256; do
    rm -f "$HOME/.local/share/icons/hicolor/${size}x${size}/apps/$APP_NAME.png"
done
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi
echo -e "${GREEN}[✓]${NC} Removed brand icon"

rm -rf "$INSTALL_DIR"
echo -e "${GREEN}[✓]${NC} Removed app files"

echo ""
read -r -p "Remove config and keys ($CONFIG_DIR)? [y/N] " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    rm -rf "$CONFIG_DIR"
    echo -e "${GREEN}[✓]${NC} Removed config and keys"
else
    echo -e "${YELLOW}[!]${NC} Config kept at $CONFIG_DIR"
fi

echo ""
echo -e "${GREEN}${BOLD}Uninstalled.${NC}"
echo ""
