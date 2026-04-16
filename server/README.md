# Desktop Connector — Server

Config-less PHP relay server for [Desktop Connector](../README.md). Upload it to any PHP 8.0+ hosting and it works — no configuration files, no database setup, no management. The SQLite database and storage directories are created automatically on first request.

The server is a **blind relay**: it stores only encrypted blobs and device IDs. It never sees file contents, filenames, or clipboard data.

## Requirements

- PHP 8.0+
- SQLite3 extension
- mod_rewrite (Apache) or equivalent URL rewriting
- Optional: curl + openssl extensions (for FCM push wake)

## Self-hosting (shared hosting / Apache)

Upload the `server/` contents to a directory on your hosting. The router auto-detects the base path from `SCRIPT_NAME`, so it works in any subdirectory without configuration.

```
desktop-connector/
  .htaccess            — rewrites all URLs to public/index.php, blocks protected dirs
  public/
    index.php          — front controller
    .htaccess          — rewrites non-file URLs to index.php
  src/                 — protected by .htaccess (403)
  data/                — SQLite DB (auto-created)
  storage/             — encrypted blobs (auto-created)
  migrations/          — schema migrations
```

## Local development

```bash
php -S 0.0.0.0:4441 -t server/public/
```

Note: PHP's built-in server is single-threaded — one request at a time. For production, use Apache + mod_php or nginx + php-fpm.

## FCM push wake (optional)

For near-instant delivery when the Android app is in the background:

1. Place `firebase-service-account.json` and `google-services.json` in the server root
2. Both files are protected by `.htaccess` (not publicly accessible)
3. The server sends a data-only FCM message with HIGH priority on transfer completion, bypassing Android Doze mode

Without FCM, the Android app falls back to polling on screen wake.

## API

All endpoints use device token authentication via `Authorization: Bearer <token>` header.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | /api/devices/register | No | Register device |
| GET | /api/health | Optional | Health check + heartbeat |
| GET | /api/devices/stats | Yes | Connection statistics |
| POST | /api/devices/fcm-token | Yes | Store FCM token |
| GET | /api/fcm/config | No | Firebase client config |
| POST | /api/pairing/request | Yes | Initiate pairing |
| GET | /api/pairing/poll | Yes | Poll for pairing requests |
| POST | /api/pairing/confirm | Yes | Confirm pairing |
| POST | /api/transfers/init | Yes | Init transfer |
| POST | /api/transfers/{id}/chunks/{i} | Yes | Upload chunk |
| GET | /api/transfers/pending | Yes | Get pending transfers |
| GET | /api/transfers/{id}/chunks/{i} | Yes | Download chunk |
| POST | /api/transfers/{id}/ack | Yes | Acknowledge receipt |
| GET | /api/transfers/sent-status | Yes | Delivery status |
| GET | /api/transfers/notify | Yes | Long poll for new transfers/deliveries |
| GET | /dashboard | No | HTML dashboard |
