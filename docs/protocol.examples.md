# Protocol canonical examples

This document captures canonical request/response examples for protocol-critical
flows. It is intentionally compact and pairs with contract tests under
`tests/protocol/`.

## 0) Authentication headers

Every endpoint below, except `POST /api/devices/register`, `GET /api/fcm/config`,
and `GET /dashboard`, requires both headers on every request:

```
X-Device-Id: 3c32f6854f2ac1305f6d14d4d95591f2
Authorization: Bearer df8a9fca7fd32f7374efda2df28ee7cf7c4ef11255d5cf188f4156f9683a09ca
```

`GET /api/health` accepts them optionally; when supplied it doubles as a heartbeat
(bumps `last_seen_at`).

## 1) Device registration

### Request
`POST /api/devices/register`

```json
{
  "public_key": "jM8S9Mebh1wX2rxrQf0iNfYobWfWkRj6k8T4l9W9xV8=",
  "device_type": "desktop"
}
```

### Success (new device, 201)

```json
{
  "device_id": "3c32f6854f2ac1305f6d14d4d95591f2",
  "auth_token": "df8a9fca7fd32f7374efda2df28ee7cf7c4ef11255d5cf188f4156f9683a09ca"
}
```

### Success (already registered, 200)

```json
{
  "device_id": "3c32f6854f2ac1305f6d14d4d95591f2",
  "auth_token": "df8a9fca7fd32f7374efda2df28ee7cf7c4ef11255d5cf188f4156f9683a09ca"
}
```

## 2) Pairing

### Request from phone
`POST /api/pairing/request`

```json
{
  "desktop_id": "desktop-device-id",
  "phone_pubkey": "xvgB7q8Qx7dOm0MufVh4m8qj2wT9sNq0rJHfQj+2m0Q="
}
```

### Response (201)

```json
{
  "status": "ok"
}
```

### Desktop poll response
`GET /api/pairing/poll`

```json
{
  "requests": [
    {
      "id": 7,
      "desktop_id": "desktop-device-id",
      "phone_id": "phone-device-id",
      "phone_pubkey": "xvgB7q8Qx7dOm0MufVh4m8qj2wT9sNq0rJHfQj+2m0Q=",
      "created_at": 1710000000,
      "claimed": 0
    }
  ]
}
```

### Confirm request (from desktop)
`POST /api/pairing/confirm`

```json
{
  "phone_id": "phone-device-id"
}
```

### Confirm response (200)

```json
{
  "status": "ok"
}
```

## 3) Transfer init / pending / sent-status / notify

### Init request
`POST /api/transfers/init`

```json
{
  "transfer_id": "tx-abc-123",
  "recipient_id": "phone-device-id",
  "encrypted_meta": "base64-ciphertext",
  "chunk_count": 3
}
```

### Init response (201)

```json
{
  "transfer_id": "tx-abc-123",
  "status": "awaiting_chunks"
}
```

### Chunk upload
`POST /api/transfers/tx-abc-123/chunks/0`

Body: raw encrypted chunk bytes (`Content-Type` is irrelevant; server reads the raw body).

Response (200):

```json
{
  "chunks_received": 1,
  "complete": false
}
```

`"complete": true` once the last chunk lands.

### Init error — quota (413 or 507)

`POST /api/transfers/init` rejects up front when the per-deployment storage quota won't fit the projected transfer. Two distinct statuses:

- **413 Payload Too Large** — the new transfer alone exceeds the quota. Terminal; clients fail the send immediately.

```json
{
  "error": "Transfer exceeds server quota"
}
```

- **507 Insufficient Storage** — the quota would fit the new transfer on its own, but not on top of everything still queued for that recipient. Transient; clients enter WAITING and retry until the queue drains (capped at 30 min).

```json
{
  "error": "Recipient storage limit exceeded"
}
```

### Cancel (sender tears down a still-delivering transfer)
`DELETE /api/transfers/tx-abc-123`

Response (200):

```json
{
  "status": "cancelled"
}
```

Returns 404 for unknown IDs *and* for transfers owned by a different sender (IDs are not enumerable).

### Chunk download
`GET /api/transfers/tx-abc-123/chunks/0`

Response (200, `Content-Type: application/octet-stream`): raw encrypted chunk bytes. No JSON envelope; decrypt client-side.

### Ack (recipient finalizes delivery)
`POST /api/transfers/tx-abc-123/ack`

Response (200):

```json
{
  "status": "deleted"
}
```

After ack the server deletes chunk files and flips `downloaded=1`, `chunks_downloaded=chunk_count`, `delivered_at=<timestamp>`.

### Pending response
`GET /api/transfers/pending`

```json
{
  "transfers": [
    {
      "transfer_id": "tx-abc-123",
      "sender_id": "desktop-device-id",
      "encrypted_meta": "base64-ciphertext",
      "chunk_count": 3,
      "created_at": 1710000100
    }
  ]
}
```

### Sent-status response
`GET /api/transfers/sent-status`

```json
{
  "transfers": [
    {
      "transfer_id": "tx-abc-123",
      "status": "pending",
      "delivery_state": "in_progress",
      "chunks_downloaded": 1,
      "chunk_count": 3,
      "created_at": 1710000100
    }
  ]
}
```

### Notify response (`test=1`)
`GET /api/transfers/notify?test=1`

```json
{
  "pending": false,
  "delivered": false,
  "download_progress": true,
  "time": 1710000200,
  "test": true,
  "sent_status": [
    {
      "transfer_id": "tx-abc-123",
      "status": "pending",
      "delivery_state": "in_progress",
      "chunks_downloaded": 1,
      "chunk_count": 3
    }
  ]
}
```

## 4) Fasttrack send / pending / ack

### Send request
`POST /api/fasttrack/send`

```json
{
  "recipient_id": "phone-device-id",
  "encrypted_data": "base64-ciphertext"
}
```

### Send response (201)

```json
{
  "message_id": 42
}
```

### Pending response
`GET /api/fasttrack/pending`

```json
{
  "messages": [
    {
      "id": 42,
      "sender_id": "desktop-device-id",
      "encrypted_data": "base64-ciphertext",
      "created_at": 1710000300
    }
  ]
}
```

### Ack response
`POST /api/fasttrack/42/ack`

```json
{
  "status": "ok"
}
```

## 5) Unified command/message semantics

### `.fn.*` transfer-file examples

- `.fn.clipboard.text` payload bytes decode to UTF-8 text.
- `.fn.clipboard.image` payload bytes remain binary image bytes.
- `.fn.unpair` carries no payload fields.

### Fasttrack find-phone / find-device examples

The wire keeps `fn=find-phone` as the canonical name. Receivers also
accept the alias `fn=find-device` (desktop M.8+); senders stay on the
legacy name until both platforms migrate. See D5 in
`docs/plans/desktop-multi-device-support.md`.

Sender → receiver:

```json
{"fn": "find-phone", "action": "start", "volume": 80, "timeout": 300}
{"fn": "find-phone", "action": "stop"}
```

Receiver → sender (heartbeats, encrypted with the same pair symkey):

```json
{"fn": "find-phone", "state": "ringing"}
{"fn": "find-phone", "state": "ringing", "lat": 50.1, "lng": 14.4, "accuracy": 12.5}
{"fn": "find-phone", "state": "stopped"}
```

`lat`/`lng`/`accuracy` are optional. Absence means "received, no GPS
fix yet" — the sender renders it as a heartbeat without coordinates.
Coordinates never appear in any log.

These map to unified message types in adapters:
- `FIND_PHONE_START` — sender's `action=start`
- `FIND_PHONE_STOP` — sender's `action=stop`
- `FIND_PHONE_LOCATION_UPDATE` — receiver's `state=ringing|stopped` (with or without coords)

## 6) Liveness probe (ping/pong)

### Desktop asks server to probe phone
`POST /api/devices/ping`

```json
{
  "target_device_id": "phone-device-id"
}
```

Response (200):

```json
{
  "online": true,
  "rtt_ms": 612,
  "via": "fcm"
}
```

`via` is one of `"fcm"` (FCM HIGH-priority wake + pong received), `"fresh"` (server
skipped FCM because `last_seen_at` was already recent), or `"timeout"` (no pong in
the 5 s window). Rate-limited to 1 call / 30 s per (sender, recipient) pair via an
atomic cooldown UPSERT — rejected pings get `429` with `Retry-After`.

### Phone acknowledges ping
`POST /api/devices/pong`

Empty body. Response (200): `{"status": "ok"}`. Auth middleware's
`last_seen_at` bump is the load-bearing side effect.

## 7) Error envelope

Every 4xx/5xx response is JSON of this shape, produced by `ErrorResponder`:

```json
{
  "error": "human-readable message"
}
```

For rate-limited pings the body also includes the retry hint (mirrored in a
`Retry-After` header):

```json
{
  "error": "Too many pings",
  "retry_after": 27
}
```

Canonical status codes:

| Status | When |
|---|---|
| 400 | Missing/invalid field, malformed JSON, path-traversal `transfer_id`, invalid chunk index |
| 401 | Missing or invalid credentials |
| 403 | Authenticated but not authorized (e.g. not the sender of a transfer) |
| 404 | Unknown transfer / chunk / resource |
| 409 | Conflict (transfer id already exists) |
| 410 | Transfer has been aborted (streaming only); `abort_reason` may be set in body |
| 413 | Transfer alone exceeds server quota (terminal) |
| 425 | Streaming download — chunk not yet stored; retry after `retry_after_ms` (also in `Retry-After` header, seconds) |
| 429 | Too many pings (includes `retry_after`) |
| 500 | Server-side invariant violation or storage failure |
| 507 | Recipient storage limit would be exceeded (transient; WAITING state) |

## 8) Streaming relay (optional, additive)

See `docs/plans/streaming-improvement.md` for the full design. The
streaming relay lets the recipient pull chunks as they land instead of
waiting for the whole upload — per-chunk ACK wipes the blob on the
server, so peak on-disk usage collapses to the in-flight window. Old
clients keep working unchanged because `/api/transfers/init` defaults
to classic.

### Health advertises capabilities
`GET /api/health`

```json
{
  "status": "ok",
  "time": 1710000000,
  "capabilities": ["stream_v1"]
}
```

When the operator disables streaming via `server/data/config.json`
(`"streamingEnabled": false`), the capability is omitted and every
`init` negotiates to `classic` regardless of the requested mode.

### Init with `mode=streaming`
`POST /api/transfers/init`

```json
{
  "transfer_id": "tx-stream-1",
  "recipient_id": "phone-device-id",
  "encrypted_meta": "base64-ciphertext",
  "chunk_count": 50,
  "mode": "streaming"
}
```

### Init response (streaming accepted, 201)

```json
{
  "transfer_id": "tx-stream-1",
  "status": "awaiting_chunks",
  "negotiated_mode": "streaming"
}
```

### Init response (streaming requested, recipient offline — downgraded, 201)

```json
{
  "transfer_id": "tx-stream-1",
  "status": "awaiting_chunks",
  "negotiated_mode": "classic"
}
```

The server downgrades to classic when the recipient's `last_seen_at`
is older than 15 s, when `streamingEnabled=false`, or when the client
passes `mode=classic` / omits `mode`.

### Download chunk not yet stored (425)
`GET /api/transfers/tx-stream-1/chunks/7`

```
HTTP/1.1 425 Too Early
Retry-After: 1
Content-Type: application/json

{
  "error": "Chunk not yet uploaded",
  "retry_after_ms": 1000
}
```

### Per-chunk ACK (streaming only)
`POST /api/transfers/tx-stream-1/chunks/7/ack`

Response (200) for a non-final chunk:

```json
{
  "status": "acked",
  "chunk_index": 7,
  "chunks_downloaded": 8
}
```

Response (200) on the final chunk — transfer is finalized in the same
call:

```json
{
  "status": "delivered"
}
```

After the ACK the chunk blob is removed from disk; subsequent GETs for
the same index return 410 Gone.

### Abort by recipient
`DELETE /api/transfers/tx-stream-1`

Response (200):

```json
{
  "status": "aborted",
  "reason": "recipient_abort"
}
```

Sender sees 410 on the next chunk upload. The row stays in
`/sent-status` with `status: "aborted"`, `delivery_state: "aborted"`,
and `abort_reason: "recipient_abort"`.

### Sender-initiated cancel (preserved shape)
`DELETE /api/transfers/tx-stream-1`

Response (200):

```json
{
  "status": "cancelled",
  "reason": "sender_abort"
}
```

The legacy `status: "cancelled"` is preserved so pre-streaming release
builds parse the response unchanged. Pass
`{"reason": "sender_failed"}` in the body to mark the transfer as a
sender-side failure (shown as `"Failed: quota_timeout"` etc. in the UI).

### Sent-status extensions

```json
{
  "transfer_id": "tx-stream-1",
  "status": "pending",
  "delivery_state": "in_progress",
  "chunks_downloaded": 24,
  "chunks_uploaded": 31,
  "chunk_count": 50,
  "mode": "streaming",
  "created_at": 1710000100
}
```

New fields: `mode` (`classic` \| `streaming`), `chunks_uploaded`
(streaming only, drives "Sending X→Y"), `abort_reason` (only when
`status == "aborted"`). Old clients ignore these.

### FCM wake types (opaque)

Data-only FCM payload. The `type` tells the recipient app which loop to
nudge; no content is leaked:

| type | Fired when |
|---|---|
| `transfer_ready` | Classic upload completed |
| `stream_ready` | Streaming transfer stored its first chunk |
| `abort` | Either party aborted — sent to the opposite side |
| `ping` | Liveness probe (existing) |
| `fasttrack` | Fasttrack message waiting (existing) |
