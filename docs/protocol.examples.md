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

### Fasttrack find-phone examples

```json
{"fn": "find-phone", "action": "start"}
{"fn": "find-phone", "action": "stop"}
{"fn": "find-phone", "state": "ringing"}
```

These map to unified message types:
- `FIND_PHONE_START`
- `FIND_PHONE_STOP`
- `FIND_PHONE_LOCATION_UPDATE`

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
| 429 | Too many pings (includes `retry_after`) |
| 500 | Server-side invariant violation or storage failure |
