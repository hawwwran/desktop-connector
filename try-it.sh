#!/bin/bash
# Live demo of Desktop Connector
# Sets up server + pre-paired sender/receiver, then lets you send photos interactively.

set -e
cd "$(cd "$(dirname "$0")" && pwd)"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

SENDER_CONFIG="/tmp/dc-demo-sender"
RECEIVER_CONFIG="/tmp/dc-demo-receiver"
SAVE_DIR="$HOME/Desktop-Connector-Demo"
SERVER_PORT=4441
SERVER_URL="http://localhost:$SERVER_PORT"

cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down...${NC}"
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
    [ -n "$RECEIVER_PID" ] && kill "$RECEIVER_PID" 2>/dev/null
    echo "Server and receiver stopped."
    echo -e "Received photos are still in: ${CYAN}$SAVE_DIR${NC}"
}
trap cleanup EXIT

# Clean previous demo state
rm -rf "$SENDER_CONFIG" "$RECEIVER_CONFIG"
rm -f ./server/data/connector.db
mkdir -p "$SAVE_DIR"

echo -e "${BOLD}Desktop Connector — Live Demo${NC}"
echo ""

# 1. Start server
echo -e "${YELLOW}[1/3] Starting PHP server on port $SERVER_PORT...${NC}"
php -S "localhost:$SERVER_PORT" -t ./server/public > /dev/null 2>&1 &
SERVER_PID=$!
sleep 1
curl -sf "$SERVER_URL/api/health" > /dev/null || { echo "Server failed to start"; exit 1; }
echo -e "  ${GREEN}Server running.${NC} Dashboard: ${CYAN}http://localhost:$SERVER_PORT/dashboard${NC}"

# 2. Pre-pair two instances
echo -e "${YELLOW}[2/3] Setting up paired devices...${NC}"
cd desktop
python3 -c "
import json, base64, sys
sys.path.insert(0, '.')
from pathlib import Path
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient

# Receiver (desktop)
rc = Config(config_dir=Path('$RECEIVER_CONFIG'))
rc.server_url = '$SERVER_URL'
rc.save_directory = '$SAVE_DIR'
rk = KeyManager(rc.config_dir)
rconn = ConnectionManager(rc.server_url, 'x', 'x')
rapi = ApiClient(rconn, rk)
r = rapi.register(rc.server_url, 'desktop')
rc.device_id = r['device_id']
rc.auth_token = r['auth_token']

# Sender (phone)
sc = Config(config_dir=Path('$SENDER_CONFIG'))
sc.server_url = '$SERVER_URL'
sk = KeyManager(sc.config_dir)
sconn = ConnectionManager(sc.server_url, 'x', 'x')
sapi = ApiClient(sconn, sk)
s = sapi.register(sc.server_url, 'phone')
sc.device_id = s['device_id']
sc.auth_token = s['auth_token']

# Pair them
sender_key = sk.derive_shared_key(rk.get_public_key_b64())
receiver_key = rk.derive_shared_key(sk.get_public_key_b64())
code = KeyManager.get_verification_code(sender_key)

sc.add_paired_device(rc.device_id, rk.get_public_key_b64(), base64.b64encode(sender_key).decode(), 'Demo-Desktop')
rc.add_paired_device(sc.device_id, sk.get_public_key_b64(), base64.b64encode(receiver_key).decode(), 'Demo-Phone')

print(f'  Sender:   {sc.device_id[:16]}...')
print(f'  Receiver: {rc.device_id[:16]}...')
print(f'  Verification code: {code}')
print(f'  E2E encryption: AES-256-GCM')
"
echo -e "  ${GREEN}Devices paired.${NC}"

# 3. Start receiver in background
echo -e "${YELLOW}[3/3] Starting receiver (headless)...${NC}"
python3 -m src.main --headless --config-dir="$RECEIVER_CONFIG" > /tmp/dc-demo-receiver.log 2>&1 &
RECEIVER_PID=$!
sleep 1
echo -e "  ${GREEN}Receiver running.${NC} Saving photos to: ${CYAN}$SAVE_DIR${NC}"
cd ..

echo ""
echo -e "${BOLD}${GREEN}=== Ready! ===${NC}"
echo ""
echo -e "  ${BOLD}Dashboard:${NC}  ${CYAN}http://localhost:$SERVER_PORT/dashboard${NC}  (open in browser)"
echo -e "  ${BOLD}Photos go to:${NC} ${CYAN}$SAVE_DIR${NC}"
echo ""
echo -e "  ${BOLD}Send a photo:${NC}"
echo -e "    ${CYAN}cd desktop && python3 -m src.main --headless --send=\"/path/to/file\" --config-dir=$SENDER_CONFIG${NC}"
echo ""
echo -e "  ${BOLD}Example with your screenshots:${NC}"
echo -e "    ${CYAN}cd desktop && python3 -m src.main --headless --send=\"$HOME/Pictures/Screenshots/test.png\" --config-dir=$SENDER_CONFIG${NC}"
echo ""
echo -e "  Send any file — photos, documents, archives, anything. Try dragging a file path into the terminal."
echo ""
echo -e "${YELLOW}Press Enter to send a photo interactively, or Ctrl+C to quit.${NC}"

# Interactive send loop
while true; do
    echo ""
    read -r -p "$(echo -e ${BOLD}Photo path \(or q to quit\): ${NC})" PHOTO_PATH

    if [ "$PHOTO_PATH" = "q" ] || [ "$PHOTO_PATH" = "Q" ]; then
        break
    fi

    # Strip quotes if user pasted a quoted path
    PHOTO_PATH="${PHOTO_PATH//\"/}"
    PHOTO_PATH="${PHOTO_PATH//\'/}"

    if [ ! -f "$PHOTO_PATH" ]; then
        echo -e "  ${YELLOW}File not found: $PHOTO_PATH${NC}"
        continue
    fi

    echo -e "  Encrypting and sending..."
    cd desktop
    if python3 -m src.main --headless --send="$PHOTO_PATH" --config-dir="$SENDER_CONFIG" 2>&1 | tail -3; then
        # Give receiver a moment to poll and download
        sleep 3
        LATEST=$(ls -t "$SAVE_DIR" | head -1)
        if [ -n "$LATEST" ]; then
            echo -e "  ${GREEN}Received: $SAVE_DIR/$LATEST${NC}"
        fi
    fi
    cd ..
done
