# Vault — `vault_v1` Byte-Exact Formats

Status: draft, frozen at T0
Audience: implementers writing a compatible client or server
Scope: the byte layout of every encrypted envelope, AAD construction, KDF parameters, and on-disk format that participates in `vault_v1`

This document is the second-implementer reference. Where the wire surface is documented in [`vault-v1.md`](vault-v1.md), this file covers what those base64 / hex / opaque byte strings actually look like. A second implementer should be able to write a compatible client or server from this doc + the wire doc + the test vectors at `tests/protocol/vault-v1/` (drafted in T0.4).

When this document disagrees with [T0 §"Implementation clarifications"](../plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md#implementation-clarifications-audit-closures-2026-05-02), T0 wins.

---

## 1. Encoding conventions

| Convention | Choice |
|---|---|
| Text encoding | UTF-8, **NFC** normalized (Unicode 16). Applies to passphrases, display names, manifest plaintext, JSON keys / values. |
| Integer endianness | **Big-endian** for everything in cryptographic constructions (AAD, length prefixes, CBOR-internal sizes). |
| Base32 alphabet | **RFC 4648** standard alphabet (A–Z, 2–7), padding **stripped**. Lowercase for chunk and other random IDs; uppercase for human-displayed Vault IDs. Decoders accept both cases case-insensitively. |
| Base64 alphabet | **RFC 4648 §4** (with `+`/`/`), padding **kept**. Used in JSON wire payloads. |
| Hex | Lowercase, no separators. SHA-256 → 64 hex chars. Device IDs → 32 hex chars (per `protocol.md` §3.4). |
| JSON canonical form | **RFC 8785 (JCS)** — sorted keys, no insignificant whitespace, fixed number formatting. Required wherever JSON is hashed or AEAD-encrypted, so identical inputs produce bit-identical ciphertexts across implementations. |
| CBOR canonical form | **RFC 8949 §4.2.1** ("Core Deterministic Encoding"). Required for the export bundle (§16). |
| Timestamps in plaintext | RFC 3339 with millisecond precision, UTC (e.g. `2026-05-02T10:30:00.000Z`). The `created_at` int form (Unix epoch seconds) is **not** used in v1 vault structures; we use RFC 3339 strings everywhere except the existing transfer pipeline. |

---

## 2. Cryptographic primitives

| Primitive | Choice | Notes |
|---|---|---|
| AEAD | **XChaCha20-Poly1305** (IETF construction) | 32-byte key, 24-byte nonce, 16-byte tag. Tag is appended to ciphertext: ciphertext_and_tag = ciphertext ‖ tag. The 24-byte nonce removes nonce-collision risk for randomly-generated nonces, important for long-lived vaults with many writes. |
| Hash | **SHA-256** | 32-byte output. |
| MAC | **HMAC-SHA256** | 32-byte output. Used for genesis fingerprint and verification code. |
| KDF (subkeys) | **HKDF-SHA256** (RFC 5869) | 32-byte output unless noted. Construction: `subkey = HKDF(salt=b"", ikm=master, info=label_utf8, L=32)`. The "no salt" extract uses a 32-byte zero string per RFC 5869 §2.2. |
| KDF (passphrase) | **Argon2id** (RFC 9106) | Locked parameters in §11. Output 32 bytes. |
| Asymmetric (DH) | **X25519** | Existing primitive from the transfer pipeline. Used for vault join QR (§13). |

Implementations should use a single audited library where possible:

- **Python**: `pynacl` (libsodium binding) supplies XChaCha20-Poly1305, X25519, BLAKE2b/Poly1305 primitives. `cryptography` covers HKDF, HMAC. `argon2-cffi` covers Argon2id. All three are already pinned in `desktop/requirements.txt` (Argon2id added in T2).
- **PHP**: libsodium (`sodium_*` builtins, available in PHP 7.2+) supplies XChaCha20-Poly1305, X25519, Argon2id. SHA-256 / HMAC / HKDF: vendor a small standalone HKDF-SHA256 implementation (see `server/src/Crypto/VaultCrypto.php`).

### 2.1 AEAD output convention

Every AEAD ciphertext on the wire follows:

```text
aead_output := ciphertext ‖ tag
            (length: plaintext_length + 16 bytes)
```

The 16-byte Poly1305 tag is the suffix. Decoders split off the last 16 bytes before passing to the AEAD library, **or** pass the full `aead_output` to libraries (e.g. libsodium) that consume tag-suffixed ciphertext directly.

### 2.2 Random sources

All nonces, salts, and random IDs MUST come from a CSPRNG (`/dev/urandom` on Linux, `getrandom`, `secrets.token_bytes` in Python, `random_bytes` in PHP, `SecureRandom` in JVM/Kotlin). Pseudo-random sources are forbidden.

---

## 3. Identifier formats

### 3.1 Vault ID

```text
12 base32 characters (RFC 4648, uppercase canonical), grouped 4-4-4 for display
example display:  ABCD-2345-WXYZ
example wire:     ABCD2345WXYZ
entropy:          60 bits
```

The dashes are display-only. On the wire and in URLs the canonical form is the 12 base32 chars without dashes, **uppercase**. Servers normalize by stripping `-` and uppercasing before matching. AAD encodings use the 12-byte UTF-8 ASCII representation of the canonical form.

### 3.2 Chunk ID

```text
^ch_v1_[a-z2-7]{24}$
total length: 30 bytes UTF-8 ASCII
entropy: 120 bits (24 base32 chars × 5 bits)
```

Per T0 §A19, the strict prefix is mandatory and the alphabet is RFC 4648 base32 **lowercase**. Server rejects deviation with 400 `vault_invalid_request` and `details.field = "chunk_id"`.

### 3.3 Other random IDs

All other client-generated random IDs use the same 30-byte format with their own 2-character prefix:

| ID | Prefix | Used for |
|---|---|---|
| chunk_id | `ch_v1_` | Chunk envelope |
| remote_folder_id | `rf_v1_` | Manifest entry: per remote folder |
| file_id | `fe_v1_` | Manifest entry: per file (path-stable) |
| file_version_id | `fv_v1_` | Manifest entry: per version inside a file |
| op_log_segment_id | `os_v1_` | Archived op-log segment (§D14) |
| join_request_id | `jr_v1_` | QR join flow (§13) |
| grant_id | `gr_v1_` | Device grant (§14) |
| recovery_envelope_id | `rk_v1_` | Recovery envelope (§12) |
| migration_token | `mt_v1_` | Migration session (§7 of vault-v1.md) |
| operation_id | `op_v1_` | Op-log entry inside a manifest |
| plan_id | `pl_v1_` | GC plan (§6.12 of vault-v1.md) |
| job_id | `jb_v1_` | Scheduled hard-purge job |

All have **30 byte UTF-8 ASCII** length, **120 bit** entropy. AAD encodings use the 30-byte UTF-8 form.

### 3.4 Device ID

Device IDs are inherited from the existing pairing protocol: 32 lowercase hex characters per [`protocol.md`](protocol.md) §3.4. AAD encodings use the 32-byte UTF-8 ASCII form.

---

## 4. Key hierarchy

```text
                       Vault Master Key (32 random bytes)
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
         HKDF-SHA256 with info label per purpose:
                    │              │              │
                    ▼              ▼              ▼
          k_header        k_manifest       k_chunk
          k_op_log        k_folder_wrap    k_device_grant_admin
          k_export                          (one per active subkey)
```

The Vault Master Key is generated client-side at `POST /api/vaults` time via CSPRNG. It is never sent to the relay. The master key is wrapped at rest:

- in the **recovery envelope** (passphrase + recovery secret protect it; §12)
- in **device grants** (per-device wrapping for the QR-join flow; §14)
- in the **device keyring / encrypted local config** for everyday use (per `desktop/src/vault.py` plan; out of scope of this document — the wrapping is local-device, not protocol-visible)

### 4.1 HKDF subkey derivation

```text
subkey = HKDF-SHA256(
  salt = b"",                               # 32 zero bytes per RFC 5869 §2.2
  ikm  = vault_master_key (32 bytes),
  info = label_utf8,
  L    = 32 bytes
)
```

Where `label_utf8` is the UTF-8 byte sequence of the label string.

### 4.2 Locked subkey labels

All labels are the literal UTF-8 string. Never trim, never re-case.

| Subkey | Label (UTF-8) | AEAD scope |
|---|---|---|
| `k_header` | `dc-vault-v1/header` | Vault header envelope (§9) |
| `k_manifest` | `dc-vault-v1/manifest` | Manifest envelope (§10) |
| `k_chunk` | `dc-vault-v1/chunk` | Chunk envelope (§11) |
| `k_op_log_segment_<segment_id>` | `dc-vault-v1/op-log-segment/<segment_id>` | One per segment (§D14, §10.4 below). The `<segment_id>` substitution is the literal 30-byte segment_id string. |
| `k_folder_wrap` | `dc-vault-v1/folder-wrap` | Reserved for v1.5 per-folder key splits — not used in v1 (folder data is encrypted under `k_manifest` directly). |
| `k_export_record` | `dc-vault-v1/export-record` | Per-record key inside the export bundle stream (§16). |
| `k_qr_verification` | `dc-vault-v1/qr-verification` | Verification code (§13.4). Derived from the X25519 shared secret, not the master key. |
| `k_device_grant_wrap` | `dc-vault-v1/device-grant-wrap` | Wrap key for device-grant material (§14). Derived from the QR X25519 shared secret. |
| `k_recovery_wrap` | `dc-vault-v1/recovery-wrap` | Wrap key for the recovery envelope (§12). Derived from Argon2id output + recovery secret. |
| `k_export_wrap` | `dc-vault-v1/export-wrap` | Wrap key for the export bundle's wrapped key envelope (§16). Derived from Argon2id output of the export passphrase. |

Future versions add labels under the same `dc-vault-v1/...` namespace; renaming or repurposing existing labels is forbidden.

---

## 5. AEAD envelope conventions

Every encrypted blob in the vault follows the same skeleton:

```text
envelope := plaintext_header ‖ nonce ‖ aead_ciphertext_and_tag
```

Where:

- **`plaintext_header`** — fixed-length, deterministic, exposes only routing/version metadata. Never carries secret content. Servers MAY parse it (e.g., to extract `revision` for CAS checks).
- **`nonce`** — 24 random bytes (CSPRNG) per envelope. Random nonces are safe at this width; never reuse a nonce with the same key.
- **`aead_ciphertext_and_tag`** — output of XChaCha20-Poly1305 with the corresponding `k_<purpose>` subkey, the random `nonce`, the envelope's `plaintext`, and the AAD constructed per §6.

Different envelope types fix different `plaintext_header` fields; the **schema string** in §6 ties an envelope type to its AAD layout.

---

## 6. AAD constructions

Every AAD is a **deterministic byte concatenation of fixed-length-encoded fields**, in the order listed. No separators. No length prefixes (lengths are all fixed). This is the construction T0 §A3 calls "concatenation, fixed-length encoding".

For each envelope type below, the AAD's total byte length is constant. A second implementer can build a bit-identical AAD given the same field values.

### 6.1 Manifest AAD (80 bytes)

```text
aad_manifest := utf8("dc-vault-manifest-v1")    # 20 bytes
             ‖ vault_id_bytes                    # 12 bytes (UTF-8, no dashes, uppercase)
             ‖ revision_be64                     # 8 bytes (big-endian uint64)
             ‖ parent_revision_be64              # 8 bytes (0x0000000000000000 for genesis)
             ‖ author_device_id_bytes            # 32 bytes (UTF-8 hex)
                                                 # total: 80 bytes
```

### 6.2 Chunk AAD (135 bytes)

```text
aad_chunk := utf8("dc-vault-chunk-v1")           # 17 bytes
          ‖ vault_id_bytes                        # 12 bytes
          ‖ remote_folder_id_bytes                # 30 bytes
          ‖ file_id_bytes                         # 30 bytes
          ‖ file_version_id_bytes                 # 30 bytes
          ‖ chunk_index_be64                      # 8 bytes
          ‖ chunk_plaintext_size_be64             # 8 bytes
                                                  # total: 135 bytes
```

### 6.3 Header AAD (38 bytes)

```text
aad_header := utf8("dc-vault-header-v1")         # 18 bytes
           ‖ vault_id_bytes                       # 12 bytes
           ‖ header_revision_be64                 # 8 bytes
                                                  # total: 38 bytes
```

### 6.4 Op-log segment AAD (68 bytes)

```text
aad_op_log_segment := utf8("dc-vault-op-log-v1") # 18 bytes
                   ‖ vault_id_bytes               # 12 bytes
                   ‖ segment_id_bytes             # 30 bytes
                   ‖ seq_be64                     # 8 bytes
                                                  # total: 68 bytes
```

### 6.5 Recovery envelope AAD (62 bytes)

```text
aad_recovery := utf8("dc-vault-recovery-v1")     # 20 bytes
             ‖ vault_id_bytes                     # 12 bytes
             ‖ envelope_id_bytes                  # 30 bytes
                                                  # total: 62 bytes
```

### 6.6 Device grant AAD (98 bytes)

```text
aad_device_grant := utf8("dc-vault-device-grant-v1") # 24 bytes
                 ‖ vault_id_bytes                     # 12 bytes
                 ‖ grant_id_bytes                     # 30 bytes (gr_v1_…)
                 ‖ claimant_device_id_bytes           # 32 bytes (UTF-8 hex)
                                                      # ────────────
                                                      # total: 98 bytes
```

### 6.7 Export key wrap AAD (35 bytes)

```text
aad_export_wrap := utf8("dc-vault-export-wrap-v1")   # 23 bytes
                ‖ vault_id_bytes                      # 12 bytes
                                                      # total: 35 bytes
```

### 6.8 Export record AAD (42 bytes)

```text
aad_export_record := utf8("dc-vault-export-record-v1") # 25 bytes
                  ‖ vault_id_bytes                       # 12 bytes
                  ‖ record_index_be32                    # 4 bytes
                  ‖ record_type_u8                       # 1 byte
                                                         # total: 42 bytes
```

> All "total" line counts are reproduced in the test vectors and assertions live in `tests/protocol/test_vault_v1_vectors.py`.

---

## 7. Format-version byte

The first byte of every envelope's `plaintext_header` is the **format version** (uint8). v1 uses `0x01` everywhere.

A reader that sees an unknown format version MUST stop **before** attempting AEAD decryption, and surface `vault_format_version_unsupported` 422 (per T0 §A3). This forces forward-compatible upgrade prompts rather than mysterious AEAD failures.

| Envelope | Version byte | Notes |
|---|---|---|
| Vault header | `0x01` | Increment for any change to the §9 layout. |
| Manifest | `0x01` | Plain `manifest_format_version` per T0 §A3 / §D1. |
| Op-log segment | `0x01` | |
| Chunk | (no version byte) | Format is bound to the chunk-id `v1` namespace; future v2 chunks use `ch_v2_…` prefix and a different envelope. |
| Recovery envelope | `0x01` | |
| Device grant | `0x01` | |
| Export bundle outer header | `0x01` | The `DCVE` magic precedes this byte. |

---

## 8. Vault Master Key

```text
vault_master_key := 32 random bytes (CSPRNG)
```

Generated once at `POST /api/vaults`. Never transmitted in plaintext. Stored:

- Wrapped in one or more **recovery envelopes** (§12), persisted in the encrypted vault header.
- Wrapped per-device in the OS keyring (or encrypted local fallback) — not protocol-visible.
- Wrapped in **device grants** during QR join (§14).

All other vault keys (`k_manifest`, `k_chunk`, …) derive from this master key via HKDF (§4).

### 8.1 Genesis fingerprint

The vault's stable cryptographic identity, used to detect "is this the same vault?" during import / merge / migration:

```text
genesis_fingerprint := HMAC-SHA256(
  key  = vault_master_key,
  data = utf8("dc-vault-v1/genesis-fingerprint")
)[0:16]    # truncated to 16 bytes / 128 bits
```

Stored as 32 hex chars in the encrypted header plaintext (§9). Two vaults with the same master key share a fingerprint; two unrelated vaults collide with probability ≤ 2⁻⁶⁴.

Compared client-side after decrypting both sides' header (per T0 §D9 import-merge / §H2 migration-verify).

---

## 9. Vault header envelope

The opaque blob returned by `GET /api/vaults/{id}/header` in `data.encrypted_header`.

### 9.1 Layout

```text
header_envelope := format_version_u8                # 1 byte (0x01)
                ‖ vault_id_bytes                    # 12 bytes
                ‖ header_revision_be64              # 8 bytes
                ‖ nonce                              # 24 bytes
                ‖ aead_ciphertext_and_tag            # variable
                                                     # ───────────
                                                     # total: 45 + N bytes
```

The relay parses the first 21 bytes (version + vault_id + header_revision) for CAS checks; never decrypts the body.

### 9.2 Plaintext (canonical JSON, RFC 8785)

```json
{
  "schema": "dc-vault-header-v1",
  "vault_id": "ABCD2345WXYZ",
  "created_at": "2026-05-02T10:00:00.000Z",
  "genesis_fingerprint": "<32 hex>",
  "kdf_profiles": {
    "recovery": "argon2id-v1",
    "export": "argon2id-v1"
  },
  "recovery_envelopes": [
    {
      "envelope_id": "rk_v1_…",
      "type": "recovery-kit-passphrase",
      "argon_salt": "<base64, 16 bytes>",
      "argon_params": {
        "memory_kib": 131072,
        "iterations": 4,
        "parallelism": 1
      },
      "nonce": "<base64, 24 bytes>",
      "aead_ciphertext_and_tag": "<base64>"
    }
  ],
  "manifest_format_version": 1,
  "header_format_version": 1
}
```

Notes:

- `vault_id` here is the wire form (no dashes, uppercase) so it round-trips cleanly through canonical JSON.
- `recovery_envelopes` is a list because v1 may carry multiple envelopes if the user re-runs recovery setup (e.g., after a passphrase change in v1.5+). v1 always writes exactly one entry on `POST /api/vaults` and never mutates this list — passphrase rotation is v1.5.
- `manifest_format_version` is **echoed** here for fast-failure on opening — clients can refuse the vault before any manifest fetch if they don't support the version. The authoritative copy lives in each manifest envelope's plaintext header (§10.1).

### 9.3 AEAD parameters

```text
key   = HKDF-SHA256(salt=b"", ikm=vault_master_key, info=utf8("dc-vault-v1/header"), L=32)
nonce = 24 random bytes (per envelope; lives in plaintext header)
aad   = aad_header (§6.3, 38 bytes)
plain = canonical_json(header_plaintext)
```

---

## 10. Manifest envelope

### 10.1 Layout

```text
manifest_envelope := format_version_u8        # 1 byte (0x01) — manifest_format_version
                  ‖ vault_id_bytes             # 12 bytes
                  ‖ revision_be64              # 8 bytes
                  ‖ parent_revision_be64       # 8 bytes
                  ‖ author_device_id_bytes     # 32 bytes
                  ‖ nonce                       # 24 bytes
                  ‖ aead_ciphertext_and_tag     # variable
                                                # ───────────
                                                # total: 85 + N bytes
```

The first 85 bytes are deterministic from the field values. The relay parses the first 61 bytes to extract revision / parent_revision for CAS checks. Per T0 §A3, `manifest_format_version` is **plaintext, not AAD-bound** — old clients reject before decryption.

### 10.2 Plaintext (canonical JSON)

```json
{
  "schema": "dc-vault-manifest-v1",
  "vault_id": "ABCD2345WXYZ",
  "revision": 42,
  "parent_revision": 41,
  "created_at": "2026-05-02T10:00:00.000Z",
  "author_device_id": "<32 hex>",
  "manifest_format_version": 1,
  "remote_folders": [
    {
      "remote_folder_id": "rf_v1_…",
      "name": "Documents",
      "state": "active",
      "retention": {
        "keep_deleted_days": 30,
        "keep_versions": 10
      },
      "ignore_patterns": [".git/", "node_modules/", "*.tmp"],
      "entries": [
        {
          "entry_id": "fe_v1_…",
          "type": "file",
          "path": "Invoices/2026/example.pdf",
          "latest_version_id": "fv_v1_…",
          "deleted": false,
          "versions": [
            {
              "version_id": "fv_v1_…",
              "created_at": "2026-05-02T10:00:00.123Z",
              "modified_at": "2026-05-02T09:50:00.000Z",
              "logical_size": 123456,
              "ciphertext_size": 124004,
              "content_fingerprint": "<base64>",
              "chunks": [
                { "chunk_id": "ch_v1_…", "index": 0, "plaintext_size": 123456, "ciphertext_size": 123500 }
              ],
              "author_device_id": "<32 hex>"
            }
          ]
        }
      ]
    }
  ],
  "operation_log_tail": [
    {
      "operation_id": "op_v1_…",
      "kind": "file_uploaded",
      "timestamp": "2026-05-02T10:00:00.123Z",
      "device_id": "<32 hex>",
      "remote_folder_id": "rf_v1_…",
      "entry_id": "fe_v1_…",
      "version_id": "fv_v1_…"
    }
  ],
  "archived_op_segments": [
    { "seq": 7, "first_ts": "2026-04-15T08:30:00.000Z", "last_ts": "2026-04-22T14:12:00.000Z", "segment_id": "os_v1_…", "hash": "<32 hex>" }
  ]
}
```

Field-by-field byte-level notes:

- `created_at` / `modified_at` / `timestamp`: RFC 3339, ms precision, UTC. Used for `(timestamp, device_id_hash)` tie-breakers per T0 §A7.
- `content_fingerprint`: 32 bytes of HMAC-SHA256 over the plaintext file (key = `HKDF(ikm=master_key, info="dc-vault-v1/content-fingerprint")`). Encoded base64 in plaintext JSON.
- `chunks[].plaintext_size` is exact; `chunks[].ciphertext_size` = plaintext_size + 16 (AEAD tag) + 0 (no per-chunk plaintext header).
- `archived_op_segments` is **newest seq first** per T0 §D14.
- The `operation_log_tail` array MUST be ≤ 1000 entries (T0 §D14 / §A13). At rollover, the writer archives the oldest 500 into a new `os_v1_…` segment in the same CAS publish.

### 10.3 AEAD parameters

```text
key   = HKDF-SHA256(salt=b"", ikm=vault_master_key, info=utf8("dc-vault-v1/manifest"), L=32)
nonce = 24 random bytes (lives in plaintext header)
aad   = aad_manifest (§6.1, 80 bytes)
plain = canonical_json(manifest_plaintext)
```

### 10.4 Op-log segment envelope

A separate envelope, written when the writer archives the oldest 500 entries. Stored in the relay's `vault_op_log_segments` table, fetched on demand via `GET /api/vaults/{id}/op-log-segments/{segment_id}` (§6.7 of vault-v1.md).

```text
op_log_segment_envelope := format_version_u8     # 1 byte (0x01)
                        ‖ vault_id_bytes           # 12 bytes
                        ‖ segment_id_bytes         # 30 bytes
                        ‖ seq_be64                 # 8 bytes
                        ‖ nonce                     # 24 bytes
                        ‖ aead_ciphertext_and_tag   # variable
                                                    # ───────────
                                                    # total: 75 + N bytes
```

Plaintext is canonical JSON: `{"schema": "dc-vault-op-log-v1", "segment_id": "...", "seq": 7, "first_ts": "...", "last_ts": "...", "entries": [<op-log entry shape, same as manifest's tail>]}`.

```text
key   = HKDF-SHA256(salt=b"", ikm=vault_master_key,
                    info=utf8("dc-vault-v1/op-log-segment/" + segment_id_text), L=32)
nonce = 24 random bytes
aad   = aad_op_log_segment (§6.4, 68 bytes)
plain = canonical_json(segment_plaintext)
```

The per-segment subkey label binds key derivation to the segment id, so a stored segment can't be replayed under a different id.

---

## 11. Chunk envelope

Stored at `server/storage/vaults/<vault_id>/<chunk_id_prefix>/<chunk_id>` (T0 §D13). The chunk envelope has **no version byte** — chunk format is bound by the `ch_v1_…` namespace; a v2 chunk uses `ch_v2_…`.

### 11.1 Layout

```text
chunk_envelope := nonce                          # 24 bytes
               ‖ aead_ciphertext_and_tag          # variable
                                                  # ───────────
                                                  # total: 24 + N bytes
```

Where N = plaintext_size + 16 (AEAD tag).

### 11.2 AEAD parameters

```text
key   = HKDF-SHA256(salt=b"", ikm=vault_master_key, info=utf8("dc-vault-v1/chunk"), L=32)
nonce = 24 random bytes
aad   = aad_chunk (§6.2, 135 bytes)
plain = chunk_plaintext_bytes
```

### 11.3 Chunk size

- **Target plaintext chunk size: 2 MiB** (`2 * 1024 * 1024`), matching the existing transfer pipeline's `CHUNK_SIZE`.
- Final chunk of a file: any size ≤ 2 MiB (no padding).
- Hard relay-side maximum: **8 MiB** plaintext per chunk (so 8 MiB + 16 tag + 24 nonce = 8 388 648 bytes envelope). Rejected with `payload_too_large` 413 if exceeded.

The same chunk is **never** re-encrypted under different parameters: chunk-content dedup happens by chunk_id (random per upload), so the same plaintext encrypted twice produces two distinct ciphertexts and two distinct chunk_ids. (There is no plaintext-keyed dedup in v1; T0 §"Chunk ID" lock.)

### 11.4 Idempotency

Same `chunk_id` + same exact ciphertext bytes → 200 OK no-op (per `vault-v1.md` §6.8). Same `chunk_id` + different ciphertext → 422 `vault_chunk_size_mismatch` or `vault_chunk_tampered`.

---

## 12. Recovery envelope

Wraps the Vault Master Key with material derived from the **recovery passphrase** (something the user remembers) **and** the **recovery secret** (something the user has, in the recovery kit file). Both are required.

### 12.1 Recovery secret

```text
recovery_secret := 32 random bytes (CSPRNG)
```

Generated client-side at vault create. Persisted in the **recovery kit file** (§12.5), never on the relay.

### 12.2 Argon2id parameters (locked for v1)

```text
memory:      131 072 KiB  (= 128 MiB)
iterations:  4
parallelism: 1
output:      32 bytes
salt:        16 random bytes per envelope
```

Rationale: ~1 second on a 2026-era laptop CPU, expensive enough to deter offline attacks against an exfiltrated header. Memory of 128 MiB is the libsodium "moderate" preset; "sensitive" (256 MiB) is reserved for v1.5+ when we can verify low-end devices can run it.

The exact parameter values **MUST** be persisted inside each recovery envelope (per §9.2 plaintext schema) so future versions can ratchet costs up while old envelopes still open.

### 12.3 Wrap-key derivation

```text
argon_output  = argon2id(
                  password = utf8_nfc(recovery_passphrase),
                  salt     = argon_salt,                     # 16 bytes from envelope
                  m_kib    = 131072,
                  t        = 4,
                  p        = 1,
                  output_length = 32
                )

k_recovery_wrap = HKDF-SHA256(
                    salt = argon_output,                     # 32 bytes from Argon2id
                    ikm  = recovery_secret,                  # 32 bytes from recovery kit
                    info = utf8("dc-vault-v1/recovery-wrap"),
                    L    = 32
                  )
```

Both passphrase and recovery secret are required to derive `k_recovery_wrap`. Compromise of one without the other does not break the envelope.

### 12.4 Envelope layout

```text
recovery_envelope := format_version_u8           # 1 byte (0x01)
                  ‖ vault_id_bytes                # 12 bytes
                  ‖ envelope_id_bytes             # 30 bytes
                  ‖ argon_salt                    # 16 bytes
                  ‖ nonce                          # 24 bytes
                  ‖ aead_ciphertext_and_tag        # 48 bytes (32-byte master key + 16-byte tag)
                                                   # ───────────
                                                   # total: 131 bytes
```

```text
key   = k_recovery_wrap (§12.3)
nonce = 24 random bytes (per envelope)
aad   = aad_recovery (§6.5, 62 bytes)
plain = vault_master_key (32 bytes)
```

The Argon2id parameter values are **not** in the envelope's plaintext header (they live in the JSON `recovery_envelopes` entry of the vault header — §9.2). This means the envelope is small and uniform; readers always look up params by following the envelope_id back to the JSON entry.

### 12.5 Recovery kit file

Per T0 §A11, the recovery kit is a file the user keeps. The QR is an optional rendering of it.

```text
filename: <vault-id-with-dashes>.dc-vault-recovery
example:  ABCD-2345-WXYZ.dc-vault-recovery
```

File contents are **plaintext** (the security comes from the user keeping the file safe + needing the passphrase to unlock the envelope it can decrypt):

```text
# Desktop Connector — Vault Recovery Kit
# Vault ID: ABCD-2345-WXYZ
# Created:  2026-05-02
#
# This file plus your recovery passphrase can restore the vault.
# Both are required. Lose either, and the vault cannot be recovered.
# Keep this file somewhere safe and offline (USB drive, password manager,
# printed in a safe).

vault_id: ABCD-2345-WXYZ
created_at: 2026-05-02T10:00:00.000Z
recovery_secret: <base32, 56 chars without padding>
argon_params: argon2id-v1
```

Format: UTF-8, LF line endings, key/value pairs after a `# …` comment block. The lines starting with `#` are required for human readability and are skipped by parsers.

`recovery_secret`: 32 bytes encoded as 56 base32 chars (RFC 4648, lowercase, no padding). Decoders accept upper- or lowercase, with or without spacing.

The QR-render of the kit encodes only the `recovery_secret`. The passphrase is **never** in the file or the QR.

### 12.6 24-word mnemonic

Out of scope for v1 (T0 §"Out-of-scope (audit additions)"). When added in v1.5, it will be a BIP-39-style alternative encoding of the same `recovery_secret` bytes.

---

## 13. QR-assisted device join

Adds a new device's grant to an existing vault. End-to-end forward-secret via ephemeral X25519 keypairs on each side.

### 13.1 Roles

- **Admin** device: already paired with the vault; possesses `vault_master_key`. Generates the QR.
- **Claimant** device: not yet paired. Receives the QR, posts a claim, receives a wrapped grant on approval.

### 13.2 QR payload

```text
vault://<relay_host>/<vault_id>/<join_request_id>/<ephemeral_admin_pubkey_b64url>?expires=<unix_ts>
```

- `<vault_id>` — display form `XXXX-XXXX-XXXX`.
- `<join_request_id>` — `jr_v1_…` (30 bytes UTF-8).
- `<ephemeral_admin_pubkey_b64url>` — 32 bytes X25519 pubkey, encoded **base64url** (RFC 4648 §5) without padding (44 chars).
- `<unix_ts>` — expiry timestamp in seconds since epoch.

The admin's ephemeral private key never leaves the admin device.

### 13.3 X25519 shared secret

```text
On the admin (after seeing claimant_pubkey returned by GET /join-requests/{id}):
  shared_secret = X25519(ephemeral_admin_priv, claimant_pubkey)

On the claimant (after scanning the QR):
  shared_secret = X25519(ephemeral_claimant_priv, ephemeral_admin_pubkey)
```

Both compute the same 32-byte X25519 output.

### 13.4 Verification code

```text
code_material = HMAC-SHA256(
  key  = shared_secret,                            # 32 bytes
  data = utf8("dc-vault-v1/qr-verification")
)

code_int    = int.from_bytes(code_material[0:3], "big") % 1_000_000
code_string = format(code_int, "06d")              # zero-padded to 6 digits
display     = code_string[0:3] + "-" + code_string[3:6]    # e.g. "473-621"
```

Both devices compute the same code locally and display it. The user confirms verbally / visually that they match before the admin clicks "Approve". The server never sees the shared secret, the master key, or the verification code.

### 13.5 Wrap-key derivation

```text
k_device_grant_wrap = HKDF-SHA256(
  salt = b"",
  ikm  = shared_secret,                           # 32 bytes
  info = utf8("dc-vault-v1/device-grant-wrap"),
  L    = 32
)
```

---

## 14. Device grant envelope

The `wrapped_vault_grant` in `POST /api/vaults/{id}/join-requests/{req_id}/approve` carries the master key and metadata, sealed under `k_device_grant_wrap`.

### 14.1 Layout

```text
device_grant_envelope := format_version_u8        # 1 byte (0x01)
                      ‖ vault_id_bytes             # 12 bytes
                      ‖ grant_id_bytes             # 30 bytes (gr_v1_…)
                      ‖ claimant_pubkey            # 32 bytes (binds the grant to the claim)
                      ‖ nonce                       # 24 bytes
                      ‖ aead_ciphertext_and_tag     # variable
                                                    # ───────────
                                                    # total: 99 + N bytes
```

### 14.2 Plaintext (canonical JSON)

```json
{
  "schema": "dc-vault-device-grant-v1",
  "grant_id": "gr_v1_…",
  "vault_id": "ABCD2345WXYZ",
  "claimant_device_id": "<32 hex>",
  "approved_role": "sync",
  "granted_by_device_id": "<32 hex>",
  "granted_at": "2026-05-02T10:05:00.000Z",
  "vault_master_key": "<base64, 32 bytes>"
}
```

### 14.3 AEAD parameters

```text
key   = k_device_grant_wrap (§13.5)
nonce = 24 random bytes
aad   = aad_device_grant (§6.6, 98 bytes)
plain = canonical_json(grant_plaintext)
```

The claimant's `device_id` is in the AAD: a wrapped grant exfiltrated en route can't be replayed onto a different device, because the attacking device's `device_id` differs and AEAD verification fails.

After unwrapping, the claimant persists `vault_master_key` in its OS keyring (or encrypted local fallback) and stores the rest of the grant fields in `vault_grant_<vault_id>.json`. The ephemeral X25519 keys on both sides are zeroed and forgotten.

---

## 15. Vault access secret

The bearer capability used for `X-Vault-Authorization`. Distinct from the master key.

```text
vault_access_secret := 32 random bytes (CSPRNG)
display form on the wire (clients): base32 RFC 4648, no padding, 56 chars

vault_access_token_hash (server-stored): SHA-256(vault_access_secret)  -> 32 bytes
                                          encoded base64 in JSON
```

Created at `POST /api/vaults`. Rotation in T13 (§8.7 of vault-v1.md): atomic single-active-hash replacement (T0 §A5). Never derived from `vault_master_key` — the relay must remain plaintext-blind.

`details.kind = "vault"` on auth failures distinguishes vault-bearer mismatch from device-auth mismatch.

---

## 16. Export bundle

Streamable, self-contained protected backup of a vault. Per T0 §A10: outer envelope + AEAD-streamed body of CBOR records + footer.

### 16.1 Outer envelope

```text
outer_header := magic                        # 4 bytes "DCVE" (literal ASCII)
             ‖ format_version_u8              # 1 byte (0x01)
             ‖ argon_memory_kib_be32          # 4 bytes (e.g. 131072 = 128 MiB)
             ‖ argon_iterations_be32          # 4 bytes
             ‖ argon_parallelism_be32         # 4 bytes
             ‖ argon_salt                      # 16 bytes
             ‖ outer_nonce                     # 24 bytes
                                               # ───────────
                                               # total: 57 bytes
```

The Argon2id parameters here MAY differ from the recovery envelope's. Defaults: same as §12.2 (m=131072, t=4, p=1).

### 16.2 Wrapped key envelope

Immediately follows the outer header:

```text
wrapped_key_envelope := wrapped_key_aead_ciphertext_and_tag    # 48 bytes
                                                                # (32-byte export_file_key + 16-byte tag)
```

```text
k_export_wrap = argon2id(
                  password = utf8_nfc(export_passphrase),
                  salt     = argon_salt,                       # from outer header
                  m_kib    = argon_memory_kib,
                  t        = argon_iterations,
                  p        = argon_parallelism,
                  output_length = 32
                )

aad_export_wrap = utf8("dc-vault-export-wrap-v1") ‖ vault_id_bytes
                                                  # 23 + 12 = 35 bytes

wrapped_key = AEAD-XChaCha20-Poly1305(
                key = k_export_wrap,
                nonce = outer_nonce,                            # from outer header
                plaintext = export_file_key (32 random bytes),
                aad = aad_export_wrap
              )
```

The reader derives `k_export_wrap` from the user-entered passphrase + the outer header's params, decrypts `wrapped_key`, and obtains `export_file_key`.

Failure to decrypt → `vault_export_passphrase_invalid` (per T0 error table).

### 16.3 Record stream

After the wrapped key, the file contains a sequence of records, in order, ending with a footer:

```text
record_on_disk := ciphertext_length_be32                  # 4 bytes
               ‖ record_aead_ciphertext_and_tag           # ciphertext_length bytes
```

The reader reads 4 bytes, then `length` bytes, AEAD-decrypts, parses the resulting CBOR, repeats until the parsed record has `record_type == 6` (footer).

Per-record AEAD parameters:

```text
record_index         := 0-based record sequence number; index 0 is the first record AFTER the wrapped key
record_nonce_i       := outer_nonce XOR le_uint64_padded(record_index + 1)
                        # XOR'd into the low 8 bytes of the 24-byte nonce; high 16 bytes unchanged

aad_export_record_i  := utf8("dc-vault-export-record-v1")    # 25 bytes
                      ‖ vault_id_bytes                         # 12 bytes
                      ‖ record_index_be32                      # 4 bytes
                      ‖ record_type_u8                         # 1 byte
                                                               # total: 42 bytes

ciphertext_i         := AEAD-XChaCha20-Poly1305(
                          key = export_file_key,
                          nonce = record_nonce_i,
                          plaintext = canonical_cbor(record_payload),
                          aad = aad_export_record_i
                        )
```

Where `record_payload` is a 3-element CBOR array per T0 §A10:

```cbor
[record_type: uint, payload_length: uint, payload: bytes]
```

`record_type` values:

| value | name | payload structure |
|:---:|---|---|
| `1` | `export_header` | CBOR map (§16.4) |
| `2` | `bundle_index` | CBOR map (§16.5) |
| `3` | `manifest` | bytes — exact manifest envelope from §10.1 |
| `4` | `op_log_segment` | bytes — exact op-log-segment envelope from §10.4 |
| `5` | `chunk` | CBOR map (§16.6) |
| `6` | `footer` | CBOR map (§16.7) — closes the stream |

`payload_length` MUST equal the length of `payload` bytes (used as a redundant sanity check by readers).

### 16.4 export_header payload (CBOR map)

```text
{
  "schema": "dc-vault-export-v1",
  "vault_id": "ABCD2345WXYZ",
  "vault_genesis_fingerprint": h'<16 bytes>',
  "created_at": "2026-05-02T10:00:00.000Z",
  "source_relay_url": "https://old.example.com",
  "export_type": "full_vault",
  "header_revision": 5,
  "manifest_count": 12,
  "chunk_count": 12483,
  "op_log_segment_count": 2,
  "ciphertext_byte_total": 8589934592,
  "argon_params": {
    "memory_kib": 131072,
    "iterations": 4,
    "parallelism": 1
  }
}
```

The plaintext genesis fingerprint here is an aid to the import preview (T0 §gaps §17). The `argon_params` are **mirrored** from the outer header so the import code can show them in the preview without re-parsing the outer header.

### 16.5 bundle_index payload (CBOR map)

```text
{
  "entries": [
    {"chunk_id": "ch_v1_…", "ciphertext_size": 2097168, "hash": h'<32 bytes sha-256>', "stream_offset": 12345678},
    …
  ]
}
```

`stream_offset` is the byte offset (from the **start of the file**) where the matching `chunk` record's `record_on_disk` header begins. Used for resumable import: a reader can hash-verify a specific chunk without traversing the full stream linearly.

### 16.6 chunk payload (CBOR map)

```text
{
  "chunk_id": "ch_v1_…",
  "ciphertext_size": 2097168,
  "hash": h'<32 bytes sha-256>',
  "envelope": h'<chunk_envelope bytes per §11.1>'
}
```

`envelope` is the exact bytes that `GET /api/vaults/{id}/chunks/{chunk_id}` would return on the source relay. The hash covers the envelope bytes.

### 16.7 footer payload (CBOR map)

```text
{
  "schema": "dc-vault-export-footer-v1",
  "record_count": 12498,                             # total records, including this footer (= records before footer + 1)
  "overall_hash": h'<32 bytes sha-256>'              # SHA-256 of every byte from the start of the file through the byte
                                                     # immediately before this footer record's `ciphertext_length_be32` field
}
```

### 16.8 Verification on import

After AEAD-decrypting each record, the reader:

1. Verifies the inner CBOR `payload_length` matches the actual payload bytes length.
2. For `chunk` records: verifies `hash == sha256(envelope_bytes)` and `ciphertext_size == len(envelope_bytes)`.
3. For `manifest` / `op_log_segment` records: verifies the corresponding envelope's AEAD with the vault's master key (post-import, after the user supplied the recovery material to unlock the vault on the target side).
4. Footer: re-computes `overall_hash` over the file prefix and verifies. Mismatch → `vault_export_tampered` with `details.section = "footer"`.

Any AEAD failure during streaming → `vault_export_tampered` with `details.section ∈ {"envelope", "header", "manifest", "chunk", "index", "footer"}`.

### 16.9 Resumability

The writer fsync-writes `<dest>.dc-temp-<uuid>` and renames to the final path only after the footer record is written and the file is fsync'd (§gaps §11). Mid-write crash leaves a `.dc-temp-*` that the cleanup pass at app start removes after 24 hours.

The writer's checkpoint file at `~/.cache/desktop-connector/vault/exports/<session_id>.json` records `last_record_index` after each fsync; on resume the writer skips records ≤ `last_record_index` (§T8.1).

---

## 17. JSON canonicalization

All JSON used as AEAD plaintext or as input to a hash MUST be produced and parsed using **RFC 8785 JSON Canonicalization Scheme (JCS)**. Concretely:

- UTF-8 encoded; no UTF-8 BOM.
- Keys in **lexicographic order** of their UTF-16 code units (the default JCS rule).
- No insignificant whitespace between tokens.
- Numbers use the JCS `JSON.stringify` rules (integers render with no fractional part; floats use the shortest round-trippable IEEE 754 representation).
- Strings use the JSON.stringify-style `\uXXXX` escapes for control characters; printable Unicode is left as raw UTF-8 bytes.

Implementation pointers:

- Python: `pip install rfc8785` (or use the pure-stdlib alternative documented in `desktop/src/vault_crypto.py`).
- PHP: vendor the small canonical-encoder in `server/src/Crypto/JsonCanonical.php`.

A second implementer can swap in any RFC-8785-compliant library; round-trip vectors at `tests/protocol/vault-v1/manifest_v1.json` enforce byte-exact match.

## 18. CBOR canonicalization

All CBOR used in the export bundle MUST be produced and parsed using **RFC 8949 §4.2.1 Core Deterministic Encoding**. Concretely:

- Smallest possible integer encoding (no `0x18 00` for 0).
- Map keys in canonical order (sorted by encoded byte sequence, length-major).
- Floats use the shortest precision that preserves the value (half / single / double).
- Indefinite-length arrays / maps / strings are forbidden — always use definite-length forms.

---

## 19. Test vector format (T0 §A18)

Test vectors live at `tests/protocol/vault-v1/`. One file per primitive:

```text
manifest_v1.json
chunk_v1.json
header_v1.json
op_log_segment_v1.json        # added in T2
recovery_envelope_v1.json
device_grant_v1.json
export_bundle_v1.json
```

Each file is a JSON array of cases. Each case:

```json
{
  "name": "manifest-v1-genesis-happy-path",
  "description": "Happy-path encryption + decryption of a genesis manifest with one folder and one file.",
  "inputs": {
    "vault_master_key": "<hex 64 chars>",
    "vault_id": "ABCD2345WXYZ",
    "revision": 1,
    "parent_revision": 0,
    "author_device_id": "<hex 32 chars>",
    "nonce": "<hex 48 chars>",
    "manifest_plaintext": "<base64 of canonical JSON bytes>",
    "expected_aad": "<hex of the 80-byte AAD construction>"
  },
  "expected": {
    "envelope_bytes": "<hex of full envelope>",
    "subkey": "<hex 64 chars; HKDF output>",
    "ciphertext_and_tag": "<hex>"
  },
  "notes": "Exercises §6.1 + §10."
}
```

Negative cases use `expected.expected_error: "vault_..."` (a string from the T0 error-code table) instead of byte outputs:

```json
{
  "name": "manifest-v1-tampered-aad",
  "description": "Manifest decrypts with a tampered AAD (wrong author_device_id) — must fail closed.",
  "inputs": { "...": "..." },
  "expected": {
    "expected_error": "vault_manifest_tampered"
  }
}
```

The harness at `tests/protocol/test_vault_v1_vectors.py` (stubbed in T0.4, populated in T2) loads each file and exercises both:

- The Python primitives in `desktop/src/vault_crypto.py`.
- The PHP primitives in `server/src/Crypto/VaultCrypto.php` (via shell-out to a vector-runner CLI).

A vector that breaks one side breaks the build. This is the ground truth for "compatible second implementer".

---

## 20. References

- T0 decisions (authoritative): [`desktop-connector-vault-T0-decisions.md`](../plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md). Particularly relevant: §A3 (manifest_format_version + AAD), §A7 (tie-breaker timestamps), §A10 (export bundle), §A11 (recovery kit form), §A18 (test vector schema), §A19 (chunk ID regex), §D1 (manifest format versioning), §D14 (op-log segments).
- Wire surface: [`vault-v1.md`](vault-v1.md). The two documents form a closed pair: this file says "what the bytes mean", that file says "where the bytes go".
- Test vectors (when populated): `tests/protocol/vault-v1/`.
- Crypto sources (per-side): `desktop/src/vault_crypto.py`, `server/src/Crypto/VaultCrypto.php`.
- Base protocol: [`protocol.md`](protocol.md).
- Plan files: [`docs/plans/desktop-connector-vault-plan-md/`](../plans/desktop-connector-vault-plan-md/) — narrative architecture (01–11) plus the T0 decision lock.
