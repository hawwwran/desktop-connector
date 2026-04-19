# Protocol canonical examples

This document captures canonical request/response examples for protocol-critical
flows. It is intentionally compact and pairs with contract tests under
`tests/protocol/`.

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

### Confirm response
`POST /api/pairing/confirm`

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
