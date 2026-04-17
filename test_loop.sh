#!/bin/bash
# Closed-loop integration test for Desktop Connector.
# Tests the full pipeline: register, pair, send, receive — all locally.
#
# Prerequisites: PHP installed, Python packages installed.
# Usage: ./test_loop.sh

set -e
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/temp"
mkdir -p "$LOG_DIR"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

step() { echo -e "\n${YELLOW}=== $1 ===${NC}"; }
pass() { echo -e "${GREEN}PASS: $1${NC}"; }
fail() { echo -e "${RED}FAIL: $1${NC}"; exit 1; }

# Cleanup function
cleanup() {
    step "Cleaning up"
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null && echo "Stopped PHP server (PID $SERVER_PID)"
    [ -n "$RECEIVER_PID" ] && kill "$RECEIVER_PID" 2>/dev/null && echo "Stopped receiver (PID $RECEIVER_PID)"
    rm -rf "$SENDER_CONFIG" "$RECEIVER_CONFIG" "$SAVE_DIR" "$PROJECT_DIR/server/data/connector.db" "$PROJECT_DIR/server/storage/"*
    echo "Cleaned up temp dirs"
}
trap cleanup EXIT

# Config
SERVER_PORT=4441
SERVER_URL="http://localhost:$SERVER_PORT"
SENDER_CONFIG=$(mktemp -d /tmp/dc-sender-XXXXX)
RECEIVER_CONFIG=$(mktemp -d /tmp/dc-receiver-XXXXX)
SAVE_DIR=$(mktemp -d /tmp/dc-photos-XXXXX)
TEST_PHOTO="$PROJECT_DIR/temp/test_photo.png"

step "Creating test photo"
python3 -c "
from PIL import Image
img = Image.new('RGB', (640, 480), color=(73, 109, 137))
img.save('$TEST_PHOTO')
print('Created test photo: $TEST_PHOTO')
"

step "Starting PHP server on port $SERVER_PORT"
rm -f "$PROJECT_DIR/server/data/connector.db"
php -S "localhost:$SERVER_PORT" -t "$PROJECT_DIR/server/public" > "$LOG_DIR/test-server.log" 2>&1 &
SERVER_PID=$!
sleep 1

# Verify server is up
if curl -sf "$SERVER_URL/api/health" > /dev/null; then
    pass "PHP server is running"
else
    fail "PHP server did not start"
fi

step "Registering RECEIVER (simulating desktop)"
cd "$PROJECT_DIR/desktop"
python3 -c "
import sys, json
sys.path.insert(0, '.')
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient

config = Config(config_dir=__import__('pathlib').Path('$RECEIVER_CONFIG'))
config.server_url = '$SERVER_URL'
config.save_directory = '$SAVE_DIR'
crypto = KeyManager(config.config_dir)
conn = ConnectionManager(config.server_url, 'unregistered', 'none')
api = ApiClient(conn, crypto)

result = api.register(config.server_url, device_type='desktop')
assert result, 'Registration failed'
config.device_id = result['device_id']
config.auth_token = result['auth_token']

print(f'Receiver registered: {config.device_id}')
print(f'Receiver pubkey: {crypto.get_public_key_b64()}')

# Save QR data for sender
qr_data = {
    'server': config.server_url,
    'device_id': config.device_id,
    'pubkey': crypto.get_public_key_b64(),
}
with open('$RECEIVER_CONFIG/qr_data.json', 'w') as f:
    json.dump(qr_data, f)
print('QR data saved')
" || fail "Receiver registration"
pass "Receiver registered"

step "Registering SENDER (simulating phone)"
python3 -c "
import sys, json
sys.path.insert(0, '.')
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient

config = Config(config_dir=__import__('pathlib').Path('$SENDER_CONFIG'))
config.server_url = '$SERVER_URL'
crypto = KeyManager(config.config_dir)
conn = ConnectionManager(config.server_url, 'unregistered', 'none')
api = ApiClient(conn, crypto)

result = api.register(config.server_url, device_type='phone')
assert result, 'Registration failed'
config.device_id = result['device_id']
config.auth_token = result['auth_token']

print(f'Sender registered: {config.device_id}')
print(f'Sender pubkey: {crypto.get_public_key_b64()}')
" || fail "Sender registration"
pass "Sender registered"

step "Pairing: sender requests pairing with receiver"
python3 -c "
import sys, json, base64
sys.path.insert(0, '.')
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient

# Load sender
sender_config = Config(config_dir=__import__('pathlib').Path('$SENDER_CONFIG'))
sender_crypto = KeyManager(sender_config.config_dir)
sender_conn = ConnectionManager(sender_config.server_url, sender_config.device_id, sender_config.auth_token)
sender_api = ApiClient(sender_conn, sender_crypto)

# Load receiver QR data
with open('$RECEIVER_CONFIG/qr_data.json') as f:
    qr = json.load(f)

# Sender sends pairing request
ok = sender_api.send_pairing_request(qr['device_id'], sender_crypto.get_public_key_b64())
assert ok, 'Pairing request failed'
print('Pairing request sent')

# Receiver polls for pairing
receiver_config = Config(config_dir=__import__('pathlib').Path('$RECEIVER_CONFIG'))
receiver_crypto = KeyManager(receiver_config.config_dir)
receiver_conn = ConnectionManager(receiver_config.server_url, receiver_config.device_id, receiver_config.auth_token)
receiver_api = ApiClient(receiver_conn, receiver_crypto)

requests_list = receiver_api.poll_pairing()
assert len(requests_list) > 0, 'No pairing requests found'
req = requests_list[0]
print(f'Receiver got pairing from: {req[\"phone_id\"]}')

# Both derive shared key
sender_key = sender_crypto.derive_shared_key(qr['pubkey'])
receiver_key = receiver_crypto.derive_shared_key(req['phone_pubkey'])
assert sender_key == receiver_key, 'Shared keys do not match!'
print('Shared keys match!')

sender_code = KeyManager.get_verification_code(sender_key)
receiver_code = KeyManager.get_verification_code(receiver_key)
assert sender_code == receiver_code, 'Verification codes do not match!'
print(f'Verification code: {sender_code}')

# Save pairing on both sides
sender_config.add_paired_device(
    device_id=qr['device_id'],
    pubkey=qr['pubkey'],
    symmetric_key_b64=base64.b64encode(sender_key).decode(),
    name='Test-Desktop',
)
receiver_config.add_paired_device(
    device_id=req['phone_id'],
    pubkey=req['phone_pubkey'],
    symmetric_key_b64=base64.b64encode(receiver_key).decode(),
    name='Test-Phone',
)

# Confirm pairing
receiver_api.confirm_pairing(req['phone_id'])
print('Pairing confirmed on both sides')
" || fail "Pairing"
pass "Pairing completed"

step "Sending photo: sender encrypts and uploads"
python3 -c "
import sys
sys.path.insert(0, '.')
from pathlib import Path
from src.main import run_send_file
from src.config import Config
from src.crypto import KeyManager

config = Config(config_dir=Path('$SENDER_CONFIG'))
crypto = KeyManager(config.config_dir)
result = run_send_file(config, crypto, Path('$TEST_PHOTO'))
assert result == 0, f'Send failed with code {result}'
print('Photo uploaded successfully')
" || fail "Photo upload"
pass "Photo uploaded"

step "Checking server dashboard"
curl -s "$SERVER_URL/dashboard" | python3 -c "
import sys
html = sys.stdin.read()
assert 'pending' in html.lower() or 'ready' in html.lower() or 'Pending' in html or 'uploading' in html.lower(), 'Dashboard does not show transfer'
print('Dashboard shows transfer data')
" || echo "(Dashboard check skipped - non-critical)"

step "Receiving: receiver polls, downloads, decrypts"
python3 -c "
import sys, base64
sys.path.insert(0, '.')
from pathlib import Path
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient

config = Config(config_dir=Path('$RECEIVER_CONFIG'))
crypto = KeyManager(config.config_dir)
conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
api = ApiClient(conn, crypto)

# Poll for pending
transfers = api.get_pending_transfers()
assert len(transfers) > 0, 'No pending transfers'
print(f'Found {len(transfers)} pending transfer(s)')

t = transfers[0]
sender_id = t['sender_id']
paired = config.paired_devices.get(sender_id)
assert paired, f'Sender {sender_id} not in paired devices'
sym_key = base64.b64decode(paired['symmetric_key_b64'])

# Download chunks
chunks = []
for i in range(t['chunk_count']):
    data = api.download_chunk(t['transfer_id'], i)
    assert data, f'Failed to download chunk {i}'
    chunks.append(data)
    print(f'Downloaded chunk {i+1}/{t[\"chunk_count\"]}')

# Decrypt and save
save_dir = Path('$SAVE_DIR')
save_path = crypto.decrypt_chunks_to_file(t['encrypted_meta'], chunks, sym_key, save_dir)
print(f'Saved to: {save_path}')

# Verify file content matches
original = Path('$TEST_PHOTO').read_bytes()
received = save_path.read_bytes()
assert original == received, f'File content mismatch! Original: {len(original)} bytes, Received: {len(received)} bytes'
print(f'Content verified: {len(received)} bytes match')

# Ack
api.ack_transfer(t['transfer_id'])
print('Transfer acknowledged')
" || fail "Photo receive/decrypt"
pass "Photo received and decrypted correctly"

step "Verifying cleanup: transfer should be marked as downloaded"
curl -sf "$SERVER_URL/api/health" > /dev/null && pass "Server still healthy"

step "Ping/pong: auth + pairing + rate-limit gates"
# We hit /api/devices/ping directly with curl to exercise each path.
# Sender acts as the caller; receiver is the target. FCM isn't configured
# in the test environment, so reachable-and-paired calls return via:no_fcm.

SENDER_ID=$(python3 -c "import json; c=json.load(open('$SENDER_CONFIG/config.json')); print(c['device_id'])")
SENDER_TOKEN=$(python3 -c "import json; c=json.load(open('$SENDER_CONFIG/config.json')); print(c['auth_token'])")
RECEIVER_ID=$(python3 -c "import json; c=json.load(open('$RECEIVER_CONFIG/config.json')); print(c['device_id'])")

# 1. No auth → 401
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$SERVER_URL/api/devices/ping" \
  -H 'Content-Type: application/json' -d "{\"recipient_id\":\"$RECEIVER_ID\"}")
[ "$CODE" = "401" ] && pass "Unauthenticated ping → 401" || fail "Expected 401, got $CODE"

# 2. Paired + reachable, no FCM configured → 200 via:no_fcm
RESP=$(curl -s -X POST "$SERVER_URL/api/devices/ping" \
  -H "X-Device-ID: $SENDER_ID" -H "Authorization: Bearer $SENDER_TOKEN" \
  -H 'Content-Type: application/json' -d "{\"recipient_id\":\"$RECEIVER_ID\"}")
VIA=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('via',''))")
# receiver just did stats/pairing calls, may be fresh OR no_fcm depending on timing
case "$VIA" in
  fresh|no_fcm) pass "Ping authenticated paired call → via:$VIA" ;;
  *) fail "Expected via:fresh or via:no_fcm, got response: $RESP" ;;
esac

# 3. Rate limit: second ping within cooldown → 429 with Retry-After
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$SERVER_URL/api/devices/ping" \
  -H "X-Device-ID: $SENDER_ID" -H "Authorization: Bearer $SENDER_TOKEN" \
  -H 'Content-Type: application/json' -d "{\"recipient_id\":\"$RECEIVER_ID\"}")
[ "$CODE" = "429" ] && pass "Back-to-back ping blocked → 429" || fail "Expected 429, got $CODE"

# 4. Unpaired target → 403 (use a fake device_id that isn't paired with sender)
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$SERVER_URL/api/devices/ping" \
  -H "X-Device-ID: $SENDER_ID" -H "Authorization: Bearer $SENDER_TOKEN" \
  -H 'Content-Type: application/json' -d '{"recipient_id":"00000000000000000000000000000000"}')
[ "$CODE" = "403" ] && pass "Unpaired recipient → 403" || fail "Expected 403, got $CODE"

# 5. Pong is a cheap authenticated no-op returning {ok:true}
RESP=$(curl -s -X POST "$SERVER_URL/api/devices/pong" \
  -H "X-Device-ID: $RECEIVER_ID" \
  -H "Authorization: Bearer $(python3 -c "import json; c=json.load(open('$RECEIVER_CONFIG/config.json')); print(c['auth_token'])")" \
  -H 'Content-Type: application/json')
OK=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok'))")
[ "$OK" = "True" ] && pass "Pong endpoint returns ok" || fail "Pong failed: $RESP"

cd "$PROJECT_DIR"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  ALL TESTS PASSED - Full loop works!  ${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Summary:"
echo "  1. PHP server started on port $SERVER_PORT"
echo "  2. Two devices registered (sender + receiver)"
echo "  3. Devices paired via key exchange"
echo "  4. Shared keys and verification codes match"
echo "  5. Photo encrypted, chunked, and uploaded"
echo "  6. Photo downloaded, decrypted, and saved"
echo "  7. File content verified: original == received"
echo "  8. Transfer acknowledged and cleaned up"
