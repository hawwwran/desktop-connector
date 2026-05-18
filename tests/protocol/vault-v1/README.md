# Vault v1 — Cross-platform Test Vectors

JSON test cases exercised by both the desktop Python crypto (`desktop/src/vault_crypto.py`) and the server PHP crypto (`server/src/Crypto/VaultCrypto.php`). A vector that breaks one side breaks the build.

Schema lock: T0 §A18 (`temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md`). Byte format reference: [`docs/protocol/vault-v1-formats.md`](../../../docs/protocol/vault-v1-formats.md). Architecture overview: [`docs/vault-architecture.md`](../../../docs/vault-architecture.md).

## Files

One file per primitive:

| File | Primitive | Spec |
|---|---|---|
| `root_v1.json` | Root manifest envelope (metadata + folder pointers) | formats §10.A |
| `shard_v1.json` | Folder shard envelope (per-folder file entries) | formats §10.B |
| `chunk_v1.json` | Chunk envelope | formats §11 |
| `header_v1.json` | Vault header envelope | formats §9 |
| `recovery_envelope_v1.json` | Recovery envelope | formats §12 |
| `device_grant_v1.json` | Device grant envelope | formats §14 |
| `export_bundle_v1.json` | Export bundle (outer + records) | formats §16 |
| `content_fingerprint_v1.json` | Keyed file content fingerprint HMAC | formats §10.B.2 |

The single legacy `manifest_v1.json` file was replaced by `root_v1.json` + `shard_v1.json` on 2026-05-16 as part of the manifest-sharding work (use case B — multi-folder vaults). vault_v1 had never shipped; the wire format was altered in place. The transitional `manifest_v1.json` fixture was deleted in Phase H step 7f alongside the legacy single-envelope `Vault.publish_manifest` / `decrypt_manifest` test paths.

`op_log_segment_v1.json` is **deferred to v1.5** — segment archival is not emitted by v1 writers (the in-root / in-shard op log tail carries the lifetime), so neither the JSON vectors nor `GET /api/vaults/{id}/op-log-segments/{segment_id}` are wired in v1. F-T17.

## Case shape

Each file is a JSON array of cases. Each case:

```json
{
  "name": "manifest-v1-genesis-happy-path",
  "description": "One-line intent.",
  "inputs": {
    "vault_master_key": "<hex>",
    "...": "..."
  },
  "expected": {
    "envelope_bytes": "<hex>",
    "...": "..."
  },
  "notes": "Optional."
}
```

Negative cases use `expected.expected_error: "vault_..."` (a code from the T0 error table) instead of byte outputs.

### Negative-case `tamper` block

A negative vector may carry a `tamper` object inside `inputs` that tells the cross-runtime harness *how* to corrupt an otherwise-valid envelope before decrypt. Multiple keys can appear in the same `tamper` object; the harness applies them all before the AEAD step so a single vector can exercise compound failure shapes.

| Field | Type | Effect on decrypt input |
|---|---|---|
| `envelope_byte_xor` | `{ offset: int, mask: "<hex>" }` | XOR `mask` (one byte) into the envelope ciphertext at `offset` so the AEAD tag check fails. Use this for "one-byte-flip rejected" tests. |
| `wrapped_key_byte_xor` | `{ offset: int, mask: "<hex>" }` | Same shape, but applied to the export-bundle wrapped-key blob — exercises the "wrapped key tampered" path independently from the body ciphertext. |
| `aad_override` | `"<hex>"` | Replace the AAD that the decrypter normally derives from envelope-prefix fields with this literal blob, so the AEAD bind-check fails. Use for "AAD field rebind rejected" / cross-envelope-type substitution tests. |
| `decrypt_passphrase_override` | `"<utf-8 string>"` | For export-bundle / recovery-envelope vectors only: the harness derives the unwrap key from this passphrase instead of the one in `inputs.passphrase`, so the wrapped-key AEAD fails with the expected `vault_recovery_passphrase_wrong` / `vault_export_passphrase_wrong` code. |

Vectors that don't tamper omit the `tamper` field entirely. A vector with `tamper` set MUST also specify `expected.expected_error` — the harness asserts the decrypt fails with that code rather than producing plaintext.

## Running

```bash
pytest tests/protocol/test_vault_v1_vectors.py
```

The harness in `tests/protocol/test_vault_v1_vectors.py` discovers all `*.json` files in this directory, validates the case schema, and (once T2 lands) exercises both the Python and PHP primitives against each case.

In T0.4 the files are empty arrays; the harness reports `0 vectors loaded` without crashing. T2 fills them in and wires up the Python+PHP side.
