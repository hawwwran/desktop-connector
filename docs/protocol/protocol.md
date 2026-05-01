# Desktop Connector Protocol

Status: draft, reverse-specified from the current implementation on `main`  
Audience: server, desktop, and Android maintainers  
Scope: wire protocol, cryptographic envelope, state transitions, and compatibility expectations

---

## 1. Goals

Desktop Connector is a point-to-point connected-device protocol for:

- pairing registered Android devices and Linux desktops
- end-to-end encrypted file transfer through a blind relay server
- end-to-end encrypted lightweight command delivery through the `fasttrack` channel
- optional FCM-assisted wake-up for low-latency delivery and liveness probing

The relay server is intentionally **not** part of the trust boundary for payload confidentiality.

---

## 2. Terminology

- **Device**: one registered protocol participant. Current device types are `desktop` and `phone`.
- **Relay**: the PHP server. It stores ciphertext and routing metadata only.
- **Pairing**: process by which two registered devices derive the same symmetric key and confirm it with a verification code.
- **Transfer**: chunked encrypted file or synthetic file-like payload.
- **Fasttrack message**: small encrypted command payload stored in a queue, used for lightweight bidirectional control messages.
- **Sender / Recipient**: device roles for one transfer or fasttrack message.

---

## 3. Data model and wire conventions

### 3.1 Transport

- Protocol transport is HTTP/1.1 over HTTPS.
- Request and response bodies are JSON unless explicitly documented as binary.
- Binary chunk upload and download uses `application/octet-stream`.

### 3.2 Time

All timestamps are Unix epoch seconds in UTC.

### 3.3 Authentication

Authenticated endpoints require both headers:

```http
X-Device-ID: <device_id>
Authorization: Bearer <auth_token>
```

Authentication semantics:

- `device_id` identifies the caller.
- `auth_token` is an opaque bearer token returned at registration time.
- On successful authentication, the server updates the caller's `last_seen_at` timestamp.

### 3.4 Device identity

Each device owns a long-lived X25519 key pair.

Derived values:

- `public_key`: raw 32-byte X25519 public key, base64-encoded for transport
- `device_id`: first 32 hex characters of `SHA-256(raw_public_key_bytes)`

This means device identity is stable as long as the device's key pair is stable.

---

## 4. Cryptography

### 4.1 Pairwise shared key derivation

For a paired device pair:

1. Each device generates an X25519 key pair.
2. Each device obtains the other side's public key.
3. Both sides compute X25519 ECDH shared secret.
4. Both sides derive the symmetric transport key using HKDF-SHA256.

HKDF parameters:

- salt: `desktop-connector`
- info: `aes-256-gcm-key`
- output length: 32 bytes

Result:

- 32-byte AES-256-GCM key shared only by the paired devices

### 4.2 Verification code

To prevent silent key mismatch during pairing:

1. Compute `SHA-256(shared_key)`.
2. Take the first 3 bytes.
3. Interpret as a big-endian integer.
4. Apply modulo `1_000_000`.
5. Format as `XXX-XXX`.

This is a human confirmation code only. It is not used as key material.

### 4.3 Encrypted blob format

All encrypted payloads use AES-256-GCM and are serialized as:

```text
nonce(12 bytes) || ciphertext_and_tag
```

Properties:

- nonce length: 12 bytes
- authentication tag: standard AES-GCM tag appended by the crypto library
- associated data: none

### 4.4 Metadata envelope

File metadata is JSON, UTF-8 encoded, then encrypted using the paired symmetric key and finally base64-encoded for transport.

Wire representation:

```text
base64( nonce || aes_gcm(ciphertext || tag) )
```

### 4.5 Chunk encryption

Files are split into chunks of exactly 2 MiB except the final chunk.

Constants:

- `CHUNK_SIZE = 2 * 1024 * 1024`

For each transfer:

- a random 12-byte `base_nonce` is generated once
- per-chunk nonce is derived as:

```text
chunk_nonce = base_nonce XOR little_endian_12_byte(chunk_index)
```

Each chunk is encrypted separately using the shared symmetric key.

Important:

- the transmitted chunk blob still contains the nonce prefix
- the receiver decrypts each chunk independently

---

## 5. Pairing protocol

### 5.1 Preconditions

Before pairing, both devices must be registered and have valid credentials.

### 5.2 Desktop QR payload

The desktop displays a QR code containing compact JSON:

```json
{
  "server": "https://example.com/desktop-connector/public-base",
  "device_id": "desktop_device_id",
  "pubkey": "desktop_public_key_b64",
  "name": "Desktop name"
}
```

Field meanings:

- `server`: base server URL the phone should use
- `device_id`: desktop device ID
- `pubkey`: desktop X25519 public key, base64
- `name`: human-readable desktop name

### 5.3 Pairing flow overview

1. Desktop is already registered and displays QR payload.
2. Phone scans QR and learns server URL, desktop ID, and desktop public key.
3. Phone derives shared key using desktop public key.
4. Phone sends `/api/pairing/request` with its own public key.
5. Desktop polls `/api/pairing/poll` and receives the phone public key.
6. Desktop derives the same shared key.
7. Both devices independently compute the same verification code.
8. User confirms the code matches on both devices.
9. Desktop stores paired device info locally and calls `/api/pairing/confirm`.
10. Pairing is then active on both ends.

### 5.4 Local paired-device record

Each client stores, at minimum:

- peer `device_id`
- peer public key
- pairwise symmetric key
- optional display name

The relay server does **not** store the symmetric key.

---

## 6. Registration API

### 6.1 `POST /api/devices/register`

Registers the caller if not already registered. Registration is idempotent for the same public key.

Request body:

```json
{
  "public_key": "base64_32_byte_x25519_public_key",
  "device_type": "desktop"
}
```

`device_type` is optional and defaults to `unknown`.

Successful response, first registration:

```json
{
  "device_id": "32_hex_chars",
  "auth_token": "opaque_hex_token"
}
```

HTTP status:

- `201 Created` if a new registration was created
- `200 OK` if the device was already registered and existing credentials are returned

Validation rules:

- `public_key` must decode from base64
- decoded key length must be exactly 32 bytes

Failure response example:

```json
{
  "error": "Invalid public_key: must be 32 bytes base64-encoded"
}
```

---

## 7. Pairing APIs

### 7.1 `POST /api/pairing/request`

Authenticated. Sent by the phone to the desktop's server.

Request body:

```json
{
  "desktop_id": "desktop_device_id",
  "phone_pubkey": "phone_public_key_b64"
}
```

Behavior:

- verifies the target desktop exists
- deletes any previous unclaimed request from the same phone to the same desktop
- creates a new pairing request row

Success response:

```json
{
  "status": "ok"
}
```

Status code:

- `201 Created`

### 7.2 `GET /api/pairing/poll`

Authenticated. Called by the desktop.

Response:

```json
{
  "requests": [
    {
      "id": 123,
      "phone_id": "phone_device_id",
      "phone_pubkey": "phone_public_key_b64"
    }
  ]
}
```

Behavior:

- returns all currently unclaimed requests for this desktop ordered oldest first
- marks returned requests as `claimed = 1`

Implication:

- this endpoint is effectively a claiming queue, not a passive read

### 7.3 `POST /api/pairing/confirm`

Authenticated. Called by the desktop after human verification.

Request body:

```json
{
  "phone_id": "phone_device_id"
}
```

Behavior:

- normalizes pair ordering lexicographically as `(device_a_id, device_b_id)`
- creates the pairing if it does not already exist
- is idempotent for an already-confirmed pair

Response:

```json
{
  "status": "ok"
}
```

---

## 8. File transfer protocol

### 8.1 Transfer model

A transfer consists of:

- one transfer record
- one encrypted metadata envelope
- `chunk_count` encrypted chunk blobs
- one final recipient ACK

The relay stores:

- sender ID
- recipient ID
- encrypted metadata blob
- chunk count
- encrypted chunk files
- delivery tracking fields

The relay does **not** need plaintext file metadata or file contents.

### 8.2 Metadata plaintext schema

Before encryption, file metadata is a JSON object with this shape:

```json
{
  "filename": "example.txt",
  "mime_type": "text/plain",
  "size": 12345,
  "chunk_count": 1,
  "chunk_size": 2097152,
  "base_nonce": "base64_12_byte_nonce"
}
```

Field meanings:

- `filename`: receiver-visible filename or synthetic function filename
- `mime_type`: MIME type guessed by sender
- `size`: plaintext byte size of the full file
- `chunk_count`: number of encrypted chunks
- `chunk_size`: nominal chunk size, currently 2 MiB
- `base_nonce`: base64-encoded 12-byte nonce used for per-chunk nonce derivation

### 8.3 Special synthetic filenames

The protocol reserves filenames beginning with `.fn.` to represent function-like behavior instead of a normal file save.

Currently defined:

- `.fn.clipboard.text`
- `.fn.clipboard.image`
- `.fn.unpair`

Semantics are receiver-defined. The relay treats them as ordinary transfers.

### 8.4 `POST /api/transfers/init`

Authenticated. Creates the transfer row before chunks are uploaded.

Request body:

```json
{
  "transfer_id": "uuid-string",
  "recipient_id": "recipient_device_id",
  "encrypted_meta": "base64_encrypted_metadata_blob",
  "chunk_count": 3
}
```

Validation:

- all fields are required
- `chunk_count` must be between `1` and `500`, inclusive
- `transfer_id` must be unique

Server storage protection:

- recipient-side pending ciphertext storage is capped at 500 MiB

Success response:

```json
{
  "transfer_id": "uuid-string",
  "status": "awaiting_chunks"
}
```

Status code:

- `201 Created`

Representative failure cases:

- `400 Missing required fields`
- `400 Invalid chunk_count`
- `409 Transfer ID already exists`
- `507 Recipient storage limit exceeded`

### 8.5 `POST /api/transfers/{transfer_id}/chunks/{chunk_index}`

Authenticated. Uploads exactly one encrypted chunk blob.

Request:

- method: `POST`
- content type: `application/octet-stream`
- body: raw encrypted chunk blob `nonce || ciphertext_and_tag`

Behavior:

- verifies caller is the sender of the transfer
- validates `chunk_index` range
- stores chunk file on disk
- inserts chunk row if not already present
- increments `chunks_received` only for newly inserted chunks
- if all chunks are present, marks transfer `complete = 1`
- if FCM is configured, sends a wake event to the recipient

This endpoint is intentionally **idempotent** per `(transfer_id, chunk_index)`.

Success response:

```json
{
  "chunks_received": 3,
  "complete": true
}
```

### 8.6 `GET /api/transfers/pending`

Authenticated. Called by the recipient.

Response:

```json
{
  "transfers": [
    {
      "transfer_id": "uuid-string",
      "sender_id": "sender_device_id",
      "encrypted_meta": "base64_encrypted_metadata_blob",
      "chunk_count": 3,
      "created_at": 1713370000
    }
  ]
}
```

Selection criteria:

- `recipient_id == caller`
- `complete == 1`
- `downloaded == 0`

### 8.7 `GET /api/transfers/{transfer_id}/chunks/{chunk_index}`

Authenticated. Called by the recipient.

Success response:

- content type: `application/octet-stream`
- body: raw encrypted chunk blob

Behavior:

- verifies caller is the intended recipient
- returns the stored encrypted chunk
- updates sender-visible delivery progress by advancing `chunks_downloaded`
- **does not** mark the transfer delivered

Invariant:

- during chunk serving, `chunks_downloaded` is capped at `chunk_count - 1`
- only final ACK may raise `chunks_downloaded` to `chunk_count`

This prevents “last chunk served” from being treated as a reliable delivery completion signal.

### 8.8 `POST /api/transfers/{transfer_id}/ack`

Authenticated. Sent by the recipient only after successful decryption and local handling.

Request body:

- empty JSON or empty request body is accepted by current clients

Behavior:

- verifies caller is the intended recipient
- updates pairing statistics
- deletes stored chunk files
- deletes chunk rows
- sets:
  - `downloaded = 1`
  - `delivered_at = now`
  - `chunks_downloaded = chunk_count`

Success response:

```json
{
  "status": "deleted"
}
```

ACK is the **only authoritative delivery completion event**.

---

## 9. Delivery state model

The current protocol exposes sender-visible delivery state through `GET /api/transfers/sent-status` and optionally inline in `GET /api/transfers/notify`.

### 9.1 Wire-level sender state

For each sent transfer, the server returns:

```json
{
  "transfer_id": "uuid-string",
  "status": "pending",
  "delivery_state": "in_progress",
  "chunks_downloaded": 2,
  "chunk_count": 3,
  "created_at": 1713370000
}
```

Definitions:

- `status`
  - `uploading`: transfer row exists but not all chunks uploaded yet
  - `pending`: upload complete, recipient has not fully ACKed yet
  - `delivered`: recipient ACKed successfully
- `delivery_state`
  - `not_started`: recipient has not downloaded any chunk yet
  - `in_progress`: recipient has downloaded at least one chunk, but no ACK yet
  - `delivered`: recipient ACKed successfully

### 9.2 Delivery invariants

The current implementation relies on these invariants:

- `chunks_downloaded == chunk_count` iff `downloaded == 1`
- `downloaded == 1` iff `delivery_state == "delivered"`
- `delivery_state == "in_progress"` iff `chunks_downloaded > 0` and `downloaded == 0`

Clients should treat ACK-derived state as canonical.

---

## 10. Notification and long-poll protocol

### 10.1 `GET /api/transfers/notify`

Authenticated. Used as a long-poll wake channel.

Query parameters:

- `since=<epoch_seconds>`: delivered-at lower bound for sender-side delivery notification
- `test=1`: capability probe, returns immediately

Server behavior:

- blocks for up to 25 seconds
- checks every 500 ms
- returns early if any of these become true:
  - there is at least one pending incoming transfer for the caller
  - at least one transfer sent by the caller became delivered since `since`
  - aggregate `chunks_downloaded` across caller's sent transfers changed

Response shape:

```json
{
  "pending": false,
  "delivered": true,
  "download_progress": true,
  "time": 1713370000,
  "sent_status": [
    {
      "transfer_id": "uuid-string",
      "status": "pending",
      "delivery_state": "in_progress",
      "chunks_downloaded": 2,
      "chunk_count": 3
    }
  ]
}
```

Notes:

- `sent_status` is included only when delivery or delivery-progress changed
- `test=1` adds `{ "test": true }` and returns immediately

---

## 11. Fasttrack protocol

Fasttrack is a lightweight encrypted message queue for small bidirectional commands that do not justify full file-transfer setup.

Examples:

- find-my-phone start/stop
- GPS updates for find-my-phone
- future lightweight control-plane messages

The relay sees only:

- sender ID
- recipient ID
- opaque encrypted payload size
- timestamps

### 11.1 Payload model

Wire field:

- `encrypted_data`: opaque encrypted string, currently base64 text produced by end-to-end encryption on the client

The plaintext payload schema is intentionally not interpreted by the server.

A typical decrypted shape is conceptually:

```json
{
  "fn": "find-phone",
  "action": "start"
}
```

That structure is a client contract, not a relay contract.

### 11.2 Queue properties

- max pending messages per recipient: `100`
- expiry: `600` seconds
- ordering: oldest first by `created_at`
- deletion: explicit recipient ACK required

### 11.3 `POST /api/fasttrack/send`

Authenticated.

Request body:

```json
{
  "recipient_id": "recipient_device_id",
  "encrypted_data": "opaque_encrypted_text"
}
```

Behavior:

- verifies sender and recipient are paired
- deletes expired pending messages for that recipient
- enforces max pending limit
- inserts new message row
- if FCM is configured and the recipient has an FCM token, sends a data-only wake with:

```json
{
  "type": "fasttrack"
}
```

Success response:

```json
{
  "message_id": 42
}
```

Status code:

- `201 Created`

### 11.4 `GET /api/fasttrack/pending`

Authenticated.

Response:

```json
{
  "messages": [
    {
      "id": 42,
      "sender_id": "sender_device_id",
      "encrypted_data": "opaque_encrypted_text",
      "created_at": 1713370000
    }
  ]
}
```

Expired messages are removed before selection.

### 11.5 `POST /api/fasttrack/{id}/ack`

Authenticated.

Behavior:

- verifies the caller is the recipient of the message
- deletes the message

Success response:

```json
{
  "status": "ok"
}
```

---

## 12. Device presence and statistics

### 12.1 `GET /api/health`

Public endpoint, but if valid auth headers are present, it also updates the caller's `last_seen_at`.

Response:

```json
{
  "status": "ok",
  "time": 1713370000
}
```

### 12.2 `GET /api/devices/stats`

Authenticated.

Optional query:

- `paired_with=<device_id>` limits pending counts to one peer

Response shape:

```json
{
  "device_id": "caller_device_id",
  "device_type": "desktop",
  "registered_at": 1713370000,
  "last_seen_at": 1713370000,
  "paired_devices": [
    {
      "device_id": "peer_device_id",
      "device_type": "phone",
      "last_seen": 1713370000,
      "online": true,
      "transfers": 12,
      "bytes_transferred": 1234567,
      "paired_since": 1713360000
    }
  ],
  "pending_incoming": 1,
  "pending_outgoing": 0
}
```

Current online heuristic:

- a peer is considered `online: true` when `now - last_seen_at < 120 seconds`

### 12.3 `POST /api/devices/fcm-token`

Authenticated.

Request body:

```json
{
  "fcm_token": "firebase_token"
}
```

To clear the token, current server behavior accepts `null` as well.

Response:

```json
{
  "status": "ok"
}
```

---

## 13. Active liveness probe

This protocol supports an explicit FCM-assisted liveness probe.

### 13.1 `POST /api/devices/ping`

Authenticated.

Request body:

```json
{
  "recipient_id": "paired_device_id"
}
```

Preconditions:

- sender and recipient must already be paired

Server behavior:

1. verifies pairing
2. acquires an atomic per-(sender, recipient) cooldown slot
3. checks recipient's `last_seen_at`
4. if recipient was already fresh this second, returns immediate success with `via = "fresh"`
5. otherwise, if FCM is unavailable or recipient has no token, returns offline result
6. otherwise sends HIGH-priority FCM data message:

```json
{
  "type": "ping"
}
```

7. waits up to 5 seconds for recipient `last_seen_at` to advance

Possible success response:

```json
{
  "online": true,
  "last_seen_at": 1713370000,
  "rtt_ms": 812,
  "via": "fcm"
}
```

Possible `via` values in current implementation:

- `fresh`
- `no_fcm`
- `fcm_failed`
- `fcm_timeout`
- `fcm`

Rate limiting:

- cooldown is 30 seconds per `(sender_id, recipient_id)` pair
- concurrent ping for the same pair is rejected

Rate-limit failure:

- HTTP `429`
- `Retry-After` header present
- body contains `retry_after`

Example:

```json
{
  "error": "Rate limit: ping already in flight or too recent",
  "retry_after": 17
}
```

### 13.2 `POST /api/devices/pong`

Authenticated. Typically called by the phone immediately after receiving an FCM `ping` data message.

Response:

```json
{
  "ok": true,
  "t": 1713370000
}
```

Important:

- successful authentication already bumps `last_seen_at`
- `pong` is therefore a minimal acknowledgement endpoint

---

## 14. FCM config discovery

### 14.1 `GET /api/fcm/config`

Public endpoint. Returns client-safe Firebase configuration for dynamic Android initialization.

When FCM is unavailable:

```json
{
  "available": false
}
```

When available:

```json
{
  "available": true,
  "project_id": "firebase-project-id",
  "gcm_sender_id": "1234567890",
  "application_id": "1:1234567890:android:abcdef",
  "api_key": "AIza..."
}
```

The returned values are client identifiers, not relay secrets.

---

## 15. Error model

The protocol currently uses ad-hoc JSON error objects of the form:

```json
{
  "error": "Human-readable message"
}
```

There is currently no stable machine-readable error code field.

Common status codes:

- `400 Bad Request` — missing or malformed input
- `401 Unauthorized` — missing or invalid authentication
- `403 Forbidden` — caller is authenticated but not allowed for the requested resource
- `404 Not Found` — target object does not exist or is not visible to caller
- `409 Conflict` — duplicate transfer ID
- `429 Too Many Requests` — ping cooldown or fasttrack queue pressure
- `500 Internal Server Error` — unexpected missing chunk file or server failure
- `507 Insufficient Storage` — recipient pending storage limit exceeded

---

## 16. Relay trust boundary

The relay is allowed to know:

- device IDs
- pairing existence
- sender/recipient routing
- transfer creation times
- chunk counts
- approximate payload size from stored blob sizes
- FCM availability and tokens

The relay is not required to know and is not expected to know:

- plaintext filenames
- file contents
- clipboard contents
- fasttrack function payload contents
- symmetric keys

---

## 17. Compatibility expectations

### 17.1 Versioning reality

The current wire protocol is **implementation-defined** and not yet independently version-negotiated.

That means:

- compatibility is currently tied to the behavior of the shipping desktop, Android, and server implementations
- field additions must be backward-safe whenever possible
- field removals or semantic changes should be treated as protocol breaks

### 17.2 Backward-compatible changes

These are safe in principle:

- adding optional response fields
- adding new `.fn.*` synthetic filenames understood only by upgraded receivers
- adding new fasttrack plaintext `fn` values, because the relay treats payloads as opaque
- adding new server endpoints not used by older clients

### 17.3 Breaking changes

These should be considered protocol-breaking unless guarded by explicit rollout strategy:

- changing `device_id` derivation
- changing HKDF salt, HKDF info, or AES-GCM envelope format
- changing chunk nonce derivation
- changing metadata plaintext schema in a non-additive way
- changing ACK semantics so that `downloaded == 1` no longer means durable recipient success
- changing auth headers or bearer token semantics

### 17.4 Recommended future hardening

The next formalization step should add an explicit protocol version field in at least one of:

- QR payload
- registration response
- transfer metadata
- dedicated capability endpoint

Recommended minimum fields for a future capability handshake:

```json
{
  "protocol_version": 1,
  "features": {
    "fasttrack": true,
    "fcm": true,
    "long_poll": true,
    "delivery_progress": true
  }
}
```

---

## 18. State machines

### 18.1 Transfer lifecycle

Server-side conceptual lifecycle:

```text
init created
  -> awaiting_chunks
  -> complete
  -> recipient downloading
  -> delivered (after ACK)
  -> ciphertext deleted
```

Sender-observable lifecycle:

```text
uploading
  -> pending / not_started
  -> pending / in_progress
  -> delivered / delivered
```

### 18.2 Fasttrack lifecycle

```text
stored
  -> pending for recipient
  -> ACKed by recipient
  -> deleted
```

or

```text
stored
  -> expired
  -> deleted
```

### 18.3 Pairing lifecycle

```text
both devices registered
  -> QR scanned
  -> pairing request queued
  -> request claimed by desktop
  -> shared key derived on both sides
  -> verification code compared by user
  -> pairing confirmed
```

---

## 19. Implementation notes that are protocol-relevant

These are implementation details, but they affect interoperability and therefore matter to protocol maintainers.

- `GET /api/pairing/poll` is a destructive claim operation for unclaimed requests.
- Transfer chunk upload is idempotent for a given `(transfer_id, chunk_index)`.
- ACK deletes ciphertext immediately after successful receipt handling.
- `GET /api/health` doubles as a passive heartbeat when auth headers are included.
- Long-poll test mode uses `?test=1` and should be treated as capability probing, not as a delivery signal.
- FCM wake messages intentionally carry no sensitive payload.

---

## 20. Non-goals of this document

This document does not define:

- local desktop config file format
- local Android Room schema
- desktop UI process model
- exact plaintext schema of every current fasttrack function payload
- release packaging, install scripts, or distribution concerns

Those are adjacent system concerns, not core wire protocol.

---

## 21. Suggested next steps

To move from “reverse-specified” to “owned protocol”, the project should next define:

1. a first explicit `protocol_version`
2. stable machine-readable error codes
3. capability negotiation
4. canonical plaintext schema for fasttrack function payloads
5. explicit receiver semantics for `.fn.*` reserved filenames
