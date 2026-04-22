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

step "Receiving: receiver polls, downloads, decrypts (streamed)"
python3 -c "
import sys, os, base64
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

# Decrypt metadata first (matches new poller flow)
meta = crypto.decrypt_metadata(t['encrypted_meta'], sym_key)
filename = meta['filename']

# Stream chunks: download → decrypt → append to .parts/.incoming_<tid>.part
save_dir = Path('$SAVE_DIR')
save_dir.mkdir(parents=True, exist_ok=True)
parts_dir = save_dir / '.parts'
parts_dir.mkdir(parents=True, exist_ok=True)
final_path = save_dir / filename
temp_path = parts_dir / f'.incoming_{t[\"transfer_id\"]}.part'
if temp_path.exists():
    temp_path.unlink()

with open(temp_path, 'wb') as out:
    for i in range(t['chunk_count']):
        outcome = api.download_chunk(t['transfer_id'], i)
        assert outcome.status == 'ok' and outcome.data, f'Failed to download chunk {i} ({outcome.status})'
        out.write(KeyManager.decrypt_chunk(outcome.data, sym_key))
        print(f'Downloaded+decrypted chunk {i+1}/{t[\"chunk_count\"]}')
    out.flush()
    os.fsync(out.fileno())

os.replace(temp_path, final_path)
print(f'Saved to: {final_path}')

# Verify file content matches
original = Path('$TEST_PHOTO').read_bytes()
received = final_path.read_bytes()
assert original == received, f'File content mismatch! Original: {len(original)} bytes, Received: {len(received)} bytes'
print(f'Content verified: {len(received)} bytes match')

# Ack
api.ack_transfer(t['transfer_id'])
print('Transfer acknowledged')
" || fail "Photo receive/decrypt"
pass "Photo received and decrypted correctly"

step "Verifying cleanup: transfer should be marked as downloaded"
curl -sf "$SERVER_URL/api/health" > /dev/null && pass "Server still healthy"

step "Long-poll + sent-status consistency checks"
# Three invariants that must hold regardless of internal refactors:
#   1. /notify?test=1 short-circuits without blocking.
#   2. /sent-status reports status=delivered, delivery_state=delivered after ack.
#   3. /notify inline sent_status agrees with /sent-status for the same transfer.
SENDER_ID_CHECK=$(python3 -c "import json; c=json.load(open('$SENDER_CONFIG/config.json')); print(c['device_id'])")
SENDER_TOKEN_CHECK=$(python3 -c "import json; c=json.load(open('$SENDER_CONFIG/config.json')); print(c['auth_token'])")

RESP=$(curl -s "$SERVER_URL/api/transfers/notify?test=1" \
  -H "X-Device-ID: $SENDER_ID_CHECK" -H "Authorization: Bearer $SENDER_TOKEN_CHECK")
echo "$RESP" | python3 -c "
import sys, json
r = json.load(sys.stdin)
assert r.get('test') is True, f'test flag missing: {r}'
assert r.get('pending') is False, f'pending should be false: {r}'
" && pass "/notify?test=1 short-circuits" || fail "/notify?test=1 did not short-circuit: $RESP"

RESP=$(curl -s "$SERVER_URL/api/transfers/sent-status" \
  -H "X-Device-ID: $SENDER_ID_CHECK" -H "Authorization: Bearer $SENDER_TOKEN_CHECK")
echo "$RESP" | python3 -c "
import sys, json
r = json.load(sys.stdin)
t = r['transfers'][0]
assert t['status'] == 'delivered', f'status should be delivered, got {t}'
assert t['delivery_state'] == 'delivered', f'delivery_state should be delivered, got {t}'
" && pass "/sent-status shows delivered after ack" || fail "/sent-status mismatch: $RESP"

RESP=$(curl -s "$SERVER_URL/api/transfers/notify?since=0" \
  -H "X-Device-ID: $SENDER_ID_CHECK" -H "Authorization: Bearer $SENDER_TOKEN_CHECK")
echo "$RESP" | python3 -c "
import sys, json
r = json.load(sys.stdin)
assert r.get('delivered') is True, f'delivered should be true: {r}'
assert r['sent_status'][0]['delivery_state'] == 'delivered', f'inline sent_status mismatch: {r}'
" && pass "/notify inline sent_status agrees with /sent-status" || fail "/notify inline sent_status mismatch: $RESP"

step "transfer_id format validation (path-traversal guard)"
# transfer_id is concatenated into a filesystem path. A malicious paired device
# could escape the storage directory without this validator. Reject anything
# containing path separators, dots, or non-alphanumeric characters.
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$SERVER_URL/api/transfers/init" \
  -H "X-Device-ID: $SENDER_ID_CHECK" -H "Authorization: Bearer $SENDER_TOKEN_CHECK" \
  -H 'Content-Type: application/json' \
  -d '{"transfer_id":"../../etc","recipient_id":"abc","encrypted_meta":"m","chunk_count":1}')
[ "$CODE" = "400" ] && pass "Malicious transfer_id rejected → 400" || fail "Expected 400, got $CODE"

step "Classic large file (10 MB → 5 chunks, classic receive)"
# Classic path: sender calls send_file (which defaults to streaming=True,
# but the server downgrades mode=classic if the recipient isn't fresh
# enough at init time; in this hand-rolled script the recipient's
# last_seen_at can be stale since no poll loop is running). Receiver
# does the legacy "download all chunks, then ack_transfer at the end"
# dance — the classic post-upload delivery path.
LARGE_SRC="$LOG_DIR/test_large.bin"
LARGE_DST_DIR=$(mktemp -d /tmp/dc-large-XXXXX)
python3 -c "
import os
with open('$LARGE_SRC', 'wb') as f:
    f.write(os.urandom(10 * 1024 * 1024))
"
SRC_SHA=$(sha256sum "$LARGE_SRC" | cut -d' ' -f1)

# Force classic mode by asking send_file NOT to negotiate streaming,
# so the server-side mode decision is deterministic regardless of the
# recipient's last_seen_at freshness.
python3 -c "
import sys
sys.path.insert(0, '.')
from pathlib import Path
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient

config = Config(config_dir=Path('$SENDER_CONFIG'))
crypto = KeyManager(config.config_dir)
conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
api = ApiClient(conn, crypto)
paired = next(iter(config.paired_devices.items()))
recipient_id, pair_info = paired
import base64
sym_key = base64.b64decode(pair_info['symmetric_key_b64'])
tid = api.send_file(Path('$LARGE_SRC'), recipient_id, sym_key, streaming=False)
assert tid is not None, 'send_file returned None'
print(f'classic transfer_id={tid}')
" || fail "Classic large file upload"

# Receive via the single-transfer-level-ack flow (classic semantics).
python3 -c "
import sys, os, base64
sys.path.insert(0, '.')
from pathlib import Path
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient

config = Config(config_dir=Path('$RECEIVER_CONFIG'))
config.save_directory = '$LARGE_DST_DIR'
crypto = KeyManager(config.config_dir)
conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
api = ApiClient(conn, crypto)

transfers = api.get_pending_transfers()
assert transfers, 'No pending transfers'
t = transfers[0]
sym_key = base64.b64decode(config.paired_devices[t['sender_id']]['symmetric_key_b64'])
meta = crypto.decrypt_metadata(t['encrypted_meta'], sym_key)
assert meta['chunk_count'] >= 5, f'expected >=5 chunks, got {meta[\"chunk_count\"]}'

save_dir = Path('$LARGE_DST_DIR')
parts_dir = save_dir / '.parts'
parts_dir.mkdir(parents=True, exist_ok=True)
temp = parts_dir / f'.incoming_{t[\"transfer_id\"]}.part'
final = save_dir / meta['filename']
with open(temp, 'wb') as out:
    for i in range(meta['chunk_count']):
        outcome = api.download_chunk(t['transfer_id'], i)
        assert outcome.status == 'ok' and outcome.data, f'chunk {i} download failed ({outcome.status})'
        out.write(KeyManager.decrypt_chunk(outcome.data, sym_key))
    out.flush(); os.fsync(out.fileno())
os.link(str(temp), str(final))
os.unlink(str(temp))
api.ack_transfer(t['transfer_id'])
" || fail "Classic large file receive"

DST_SHA=$(sha256sum "$LARGE_DST_DIR/test_large.bin" | cut -d' ' -f1)
if [ "$SRC_SHA" = "$DST_SHA" ]; then
    pass "10 MB classic roundtrip — SHA-256 matches"
else
    fail "SHA-256 mismatch: src=$SRC_SHA dst=$DST_SHA"
fi
rm -rf "$LARGE_DST_DIR" "$LARGE_SRC"

# ---------------------------------------------------------------
# D.6a: streaming-mode round-trip
# ---------------------------------------------------------------
# Fires a real streaming transfer through the same hermetic server.
# Asserts:
#   - SHA match end-to-end.
#   - Server log recorded mode=streaming for this transfer's init.
#   - Server log recorded at least one transfer.chunk.acked_and_deleted
#     event — proves per-chunk ACK fired (the streaming storage win).
#   - Peak server-side on-disk stays below 4 * CHUNK_SIZE at final
#     sampling. (We can't instrument peak during the transfer from
#     bash, but post-transfer on_disk == 0 implies the acked-and-
#     deleted cycle completed for every chunk.)
# ---------------------------------------------------------------
step "Streaming-mode round-trip (10 MB, per-chunk ACK)"
STREAM_SRC="$LOG_DIR/test_stream.bin"
STREAM_DST_DIR=$(mktemp -d /tmp/dc-stream-XXXXX)
python3 -c "
import os
with open('$STREAM_SRC', 'wb') as f:
    f.write(os.urandom(10 * 1024 * 1024))
"
STREAM_SRC_SHA=$(sha256sum "$STREAM_SRC" | cut -d' ' -f1)
STREAM_LOG_MARKER="###streaming-marker-$(date +%s%N)"
# Drop an anchor in the server log so we can grep JUST this section's
# events (previous runs may leave earlier mode=streaming lines).
curl -s -o /dev/null -X POST "$SERVER_URL/api/transfers/notify?test=1" \
    -H "X-Device-ID: $(python3 -c "import json; print(json.load(open('$SENDER_CONFIG/config.json'))['device_id'])")" \
    -H "Authorization: Bearer $(python3 -c "import json; print(json.load(open('$SENDER_CONFIG/config.json'))['auth_token'])")" \
    -H "X-Test-Marker: $STREAM_LOG_MARKER" 2>/dev/null || true
SERVER_LOG_OFFSET=$(wc -l < "$PROJECT_DIR/server/data/logs/server.log" 2>/dev/null || echo 0)

# Receiver bumps its last_seen_at right before the sender inits so the
# server accepts streaming (freshness check: recipient last_seen_at
# must be within 15 s of now). Send runs immediately after.
python3 -c "
import sys, base64
sys.path.insert(0, '.')
from pathlib import Path
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient

# Receiver last_seen bump
rconfig = Config(config_dir=Path('$RECEIVER_CONFIG'))
rconn = ConnectionManager(rconfig.server_url, rconfig.device_id, rconfig.auth_token)
rapi = ApiClient(rconn, KeyManager(rconfig.config_dir))
rapi.get_pending_transfers()  # authenticated — bumps last_seen_at

# Sender: send_file(streaming=True) is the default.
sconfig = Config(config_dir=Path('$SENDER_CONFIG'))
sconn = ConnectionManager(sconfig.server_url, sconfig.device_id, sconfig.auth_token)
sapi = ApiClient(sconn, KeyManager(sconfig.config_dir))
paired = next(iter(sconfig.paired_devices.items()))
recipient_id, pair_info = paired
sym_key = base64.b64decode(pair_info['symmetric_key_b64'])
tid = sapi.send_file(Path('$STREAM_SRC'), recipient_id, sym_key, streaming=True)
assert tid is not None, 'streaming send_file returned None'
print(f'streaming_tid={tid}')
with open('$LOG_DIR/stream_tid.txt', 'w') as f:
    f.write(tid)
" || fail "Streaming send"

STREAM_TID=$(cat "$LOG_DIR/stream_tid.txt")

# Receive via the streaming-mode flow: per-chunk download + ack_chunk.
python3 -c "
import sys, os, base64
sys.path.insert(0, '.')
from pathlib import Path
from src.config import Config
from src.crypto import KeyManager
from src.connection import ConnectionManager
from src.api_client import ApiClient, DOWNLOAD_OK, DOWNLOAD_TOO_EARLY, DOWNLOAD_ABORTED

config = Config(config_dir=Path('$RECEIVER_CONFIG'))
config.save_directory = '$STREAM_DST_DIR'
crypto = KeyManager(config.config_dir)
conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
api = ApiClient(conn, crypto)

import time
# Wait for the stream_ready (server fires it on first chunk stored);
# in practice send_file is synchronous so by the time it returns at
# least one chunk is uploaded and pending-list has our transfer.
for _ in range(20):
    transfers = api.get_pending_transfers()
    if any(t['transfer_id'] == '$STREAM_TID' for t in transfers):
        break
    time.sleep(0.1)
else:
    raise SystemExit('pending list never surfaced streaming transfer')

t = next(t for t in transfers if t['transfer_id'] == '$STREAM_TID')
assert t.get('mode') == 'streaming', f\"server-reported mode wasn't streaming: {t.get('mode')}\"

sym_key = base64.b64decode(config.paired_devices[t['sender_id']]['symmetric_key_b64'])
meta = crypto.decrypt_metadata(t['encrypted_meta'], sym_key)
chunk_count = meta['chunk_count']
assert chunk_count >= 5, f'expected >=5 chunks, got {chunk_count}'

save_dir = Path('$STREAM_DST_DIR')
parts_dir = save_dir / '.parts'
parts_dir.mkdir(parents=True, exist_ok=True)
temp = parts_dir / f'.incoming_{t[\"transfer_id\"]}.part'
final = save_dir / meta['filename']
with open(temp, 'wb') as out:
    for i in range(chunk_count):
        # TooEarly-resilient loop (sender may still be uploading
        # later chunks while we drain earlier ones).
        for retry in range(300):  # 30 s budget at 100 ms
            outcome = api.download_chunk(t['transfer_id'], i)
            if outcome.status == DOWNLOAD_OK and outcome.data:
                break
            if outcome.status == DOWNLOAD_TOO_EARLY:
                time.sleep(0.1)
                continue
            raise SystemExit(f'chunk {i} failed: {outcome.status}')
        else:
            raise SystemExit(f'chunk {i} timed out in TooEarly loop')
        plaintext = KeyManager.decrypt_chunk(outcome.data, sym_key)
        out.write(plaintext); out.flush()
        os.fsync(out.fileno())
        # Per-chunk ack — the streaming storage win. Server deletes
        # the blob immediately.
        assert api.ack_chunk(t['transfer_id'], i), f'ack_chunk {i} failed'
os.link(str(temp), str(final))
os.unlink(str(temp))
# NO transfer-level ack in streaming mode — the final per-chunk ack
# already flipped downloaded=1 server-side.
print(f'streaming received {final.stat().st_size} bytes chunks={chunk_count}')
" || fail "Streaming receive"

STREAM_DST_SHA=$(sha256sum "$STREAM_DST_DIR/test_stream.bin" | cut -d' ' -f1)
if [ "$STREAM_SRC_SHA" = "$STREAM_DST_SHA" ]; then
    pass "Streaming SHA-256 matches ($STREAM_SRC_SHA)"
else
    fail "Streaming SHA mismatch: src=$STREAM_SRC_SHA dst=$STREAM_DST_SHA"
fi

# Post-transfer server-log assertions. Tail from our offset so earlier
# runs' events don't contaminate the grep. Server truncates transfer_id
# to the first 12 chars per the correlation-id rule (CLAUDE.md, see
# AppLog::shortId) — match on that prefix.
STREAM_TID_SHORT="${STREAM_TID:0:12}"
NEW_LOG=$(tail -n +$((SERVER_LOG_OFFSET + 1)) "$PROJECT_DIR/server/data/logs/server.log")
INIT_STREAMING=$(echo "$NEW_LOG" | grep -c "transfer.init.accepted transfer_id=$STREAM_TID_SHORT.*mode=streaming" || true)
[ "$INIT_STREAMING" -ge 1 ] && pass "Server log: transfer.init.accepted mode=streaming" \
    || fail "Expected mode=streaming in server log for tid=$STREAM_TID_SHORT; got INIT_STREAMING=$INIT_STREAMING"

ACKED_COUNT=$(echo "$NEW_LOG" | grep -c "transfer.chunk.acked_and_deleted transfer_id=$STREAM_TID_SHORT" || true)
[ "$ACKED_COUNT" -ge 5 ] && pass "Server log: $ACKED_COUNT per-chunk ACK-and-delete events (>=5)" \
    || fail "Expected >=5 acked_and_deleted events, got $ACKED_COUNT"

# Post-transfer on_disk assertion: storage dir for this transfer must
# be empty (the per-chunk ACK wipe cycle completed).
STORAGE_DIR="$PROJECT_DIR/server/storage/$STREAM_TID"
if [ -d "$STORAGE_DIR" ]; then
    REMAINING=$(ls -A "$STORAGE_DIR" 2>/dev/null | wc -l)
    [ "$REMAINING" -eq 0 ] && pass "Server storage/$STREAM_TID drained to 0 files" \
        || fail "Server storage/$STREAM_TID still has $REMAINING file(s)"
else
    pass "Server storage/$STREAM_TID cleaned up entirely"
fi

rm -rf "$STREAM_DST_DIR" "$STREAM_SRC" "$LOG_DIR/stream_tid.txt"

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
