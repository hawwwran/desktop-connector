# Vault v1 — independent max-effort review

**Date:** 2026-05-17
**Reviewer:** Claude (Opus 4.7, max effort), supervising 8 parallel investigators across disjoint code areas.
**Branch:** `tresor-vault` vs `main` — 260 commits, 412 files, +101 319 / −8 726 lines, 276 vault-specific files.
**Method:** independent walk of architecture + spec + code + tests, plus eight parallel deep-dive investigators on non-overlapping slices. Findings flagged as Critical were re-verified personally by reading the cited file:line.
**Lenses (in declared priority):** security > stability > reliability > recoverability.
**Stance:** explicitly skeptical of the team's own evaluation at `docs/vault-critical-risks-evaluation.md`. They stamped all 20 risks Resolved/Mitigated; this review is the second opinion.

> **Headline:** v1 is **not stamp-ready**. The crypto-vector layer is largely sound; the *control-flow around it* — chunk-state lifecycle, GC two-phase commit, eviction's admin gate, watcher cancellation, missing UI for QR-join / revocation / migration / rotation — is where the dangerous stuff lives. The architecture doc + risk-evaluation doc materially overstate the shipped state.

### Tally

- **17 Critical** (security exploit / silent data-loss / hard correctness / promised feature unwired / regression-blocking test gap)
- **37 High** (realistic-exploitation security or reliability bugs)
- **22 Medium**
- ~30 Low and Info combined

Breakdown by section:

| Section | Critical | High | Medium |
|---|:---:|:---:|:---:|
| §1 Server | 3 | 6 | 7 |
| §2 Desktop crypto | 0 | 3 | 5 |
| §3 Sync engine | 3 | 9 | 6 |
| §4 Upload/download/eviction | 0 | 5 | 4 |
| §5 Grants/recovery/import/migration | 3 | 4 | 6 |
| §6 GTK + UX | 5 | 5 | 2 |
| §7 Tests | 3 | 5 | 5 |
| §8 Cross-cutting | 0 | 0 | 5 |

### Top-line punch list (read these in the morning, decide ship/no-ship)

The six most-dangerous Criticals, ranked by blast radius:

1. **§1.C1** — Server `putChunk` against a `purged` chunk row returns 200 OK with no blob on disk. *Silent corruption — manifest references a chunk that 404s on read.*
2. **§3.C1** — Eviction stages 2–3 hard-purge without `admin`/`purge_secret`. *Sync-role caller can wipe every unexpired tombstone + every historical version. Spec & code disagree; code is the dangerous side.*
3. **§3.C3** — `scan.py` + `preflight.py` follow symlinks → exfiltrate files outside the binding root. *Symlink → /etc/shadow content uploaded with the symlink's binding-relative name.*
4. **§6.C4** — Vault Browser "Delete folder" hamburger bypasses Danger-zone guards (no typed-confirm, no fresh-unlock). *Spec demands both unconditionally.*
5. **§6.C5** — Schedule hard-purge has **no admin-role gate** (client or server). *Single most-permanent destructive op; anyone with the recovery passphrase can fire it.*
6. **§1.C2** — Server `gcExecute` leaks blob files when unlink fails or process crashes post-COMMIT. *Comment claims "next purge cleans up"; nothing does.*

Three more Criticals that block the v1 *feature claim* (the architecture doc says these exist; the code says they don't):

7. **§5.C1** — Full migration wizard doesn't exist; runner has no production driver.
8. **§5.C2** — QR-join + grant approval flows are library-only, no UI. Multi-device story collapses to recovery-kit only.
9. **§5.C3** — Multi-device migration propagation never wires `propagate_relay_migration` into any header-read path.

---

## Severity legend

- **Critical** — exploitable security bug, silent data-loss, hard correctness violation, or a v1-promised feature with no actually-wired code path. Blocks v1.
- **High** — security weakness with realistic exploitation, reliability bug a normal user will hit, or UX regression that destroys trust.
- **Medium** — narrower scope, clear fix.
- **Low** — polish, latent edge case, doc nit.
- **Info** — design observation worth knowing.

---

## §1. Server: crypto, auth, controllers, repositories, migrations, storage

The crypto primitives in `VaultCrypto.php` are correct. The `tryAtomicShardWithRootCAS` primitive (single `BEGIN IMMEDIATE`, double `changes()===1` check, single COMMIT) is race-safe. The surfaces *around* the crypto are where the bugs live.

### Critical

#### ~~§1.C1~~ — `putChunk` re-upload against a `purged`/`gc_pending` row returns 200 OK with no blob on disk
**Fix landed:** 5d3efbb 2026-05-17
**Files:** `server/src/Controllers/VaultController.php:825-891`, `server/src/Repositories/VaultChunksRepository.php:104-130,177-180`

**Approach:** In `put()`, when existing row's state is `purged` or `gc_pending` AND hash/size match, flip back to `active` and return `'created'` so the controller writes the blob. In `putChunk`, treat a `purged` row as needing fresh quota reservation (bytes were freed at gc/execute); `gc_pending` keeps its bytes accounted so no re-reservation needed.

Verified personally. The controller:
```php
if ($chunksRepo->head($vaultId, $chunkId) === null) { /* reserve quota... */ }
$result = $chunksRepo->put(...);   // returns 'created' or 'already_exists'
...
if ($result === 'created') { /* write blob */ } else { $statusCode = 200; }
```

`head()` is a state-blind `SELECT … WHERE vault_id=:v AND chunk_id=:c`. A row in state `purged` (file unlinked by `gcExecute`) or `gc_pending` passes the `head() === null` check ⇒ `put()` is called ⇒ `put()` compares only `(ciphertext_size, chunk_hash)` ⇒ byte-identical re-upload returns `'already_exists'` ⇒ controller returns **200 OK `{"stored":true}`** with no disk write and no quota re-charge.

Next `GET /chunks/{id}` returns 404 (or 200 if the GC window hasn't unlinked yet — see §1.H2). Manifest references a non-existent chunk. Silent.

**Fix:** in `put()`, when `state ∈ {gc_pending, purged}`, treat as fresh insert: flip back to `active`, re-charge `reserveCiphertextBytes`, return `'created'` so the controller forces a disk write.

#### ~~§1.C2~~ — `gcExecute` leaves blob files on disk forever when unlink fails or process crashes post-COMMIT
**Fix landed:** 639d22b 2026-05-17
**File:** `server/src/Controllers/VaultController.php:1199-1235`

**Approach:** Add `listPurged()` + `deleteIfPurged()` to `VaultChunksRepository`; run a residual-cleanup pass at the start of `gcExecute` retrying orphan unlinks and conditionally deleting the row on success. Post-commit unlink loop also removes the row when the unlink succeeds. State-guarded delete defends the §1.C1 revival race.

Verified. The transaction marks rows `state := 'purged'` + decrements `used_ciphertext_bytes`. The `unlink` loop runs *outside* the transaction. Comment at 1224 claims "leftover files are dead-weight that the next purge will clean up". **There is no cleanup loop**. The next `gcExecute` skips purged rows at line 1206. The unlink is never retried.

Persistent EBUSY / EIO → permanent on-disk leak with no scheduled reaper. Scales linearly with GC churn.

**Fix:** add an idempotent reaper — either a separate job that walks `state='purged'` rows and retries the unlink (then deletes the row), or extend `gcExecute` to start with a "clean residual" pass.

#### ~~§1.C3~~ — `migrationStart` discards regenerated tokens; a dropped 201 response permanently locks the admin out
**Fix landed:** eeee9c3 2026-05-17
**File:** `server/src/Controllers/VaultController.php:1326-1357`

**Approach:** Defer token minting until the intent state is known. Same-device retry with the same target rotates the stored hash and returns a fresh bearer (200, `token_returned=true`); cross-device retries return metadata-only; cross-target retries still 409. Added `rotateTokenHash()` to the intents repo.

Every call generates `random_bytes(19)` *before* checking whether an intent already exists. On retry (`!$result['created']`), the freshly-generated token is silently discarded and the response carries `"token": null`. If the client never received the original 201, there's no recovery path — the intent row exists, no endpoint lets the admin abandon and restart.

**Fix:** generate only when `created === true`; on retry without a stored token return a distinct 409 (`vault_migration_token_lost`) with a documented abandon-and-restart flow. Or allow `migrationStart` to rotate the stored hash on retry by the same `initiating_device`.

### High

#### ~~§1.H1~~ — No rate limit on vault auth attempts or create-vault despite spec mandate
**Fix landed:** c041b46 2026-05-17
**File:** `server/src/Auth/VaultAuthService.php:108`

**Approach:** Migration 006 adds `vault_auth_attempts`; `VaultAuthAttemptsRepository.recordAndRead` is a single atomic UPSERT (window-reset via CASE expression). VaultAuthService bills auth + create attempts before the AEAD compare; overflow returns 429 `vault_rate_limited` with `retry_after_ms` + `Retry-After` header.

Spec `docs/protocol/vault-v1.md` §10 mandates 10/min auth attempts and 5/hour create-vault, with `vault_rate_limited` + `Retry-After`. The only `VaultRateLimitedError` site in the entire codebase is `VaultGrantsController.php:157` (pending-join-count cap). The 32-byte access secret makes online brute-force impractical, but the missing limiter also means no IDS signal — a compromised paired device could hammer `vault_auth_failed` without observable rate-limit telemetry.

**Fix:** add a `vault_auth_attempts` SQLite UPSERT keyed `(device_id, vault_id)`, next to the existing `ping_rate` table (same atomic-UPSERT pattern).

#### ~~§1.H2~~ — `getChunk` bypasses chunk state — GC-window blobs are served if file still exists
**Fix landed:** 88802d3 2026-05-17
**File:** `server/src/Controllers/VaultController.php:907-932`

**Approach:** Apply `isUserVisibleChunkState` filter in `getChunk` so the row's state must be `active` or `retained` to serve content. Matches headChunk + batchHead behavior.

```php
$row = $chunksRepo->get($vaultId, $chunkId);
if ($row === null) { throw new VaultChunkMissingError($chunkId); }
$absPath = VaultStorage::root() . '/' . (string)$row['storage_path'];
if (!is_file($absPath)) { throw new VaultChunkMissingError($chunkId); }
$bytes = file_get_contents($absPath);
```

No `isUserVisibleChunkState()` filter (unlike `headChunk:949` and `batchHead`). During the GC window, GET returns 200 while HEAD returns 404 for the same id. Race condition window.

**Fix:** add `!self::isUserVisibleChunkState((string)$row['state'])` to the early-404 condition.

#### ~~§1.H3~~ — Migration `target_relay_url` validation is in `migrationCommit` only, not `migrationStart`
**Fix landed:** 08bbcd9 2026-05-17
**File:** `server/src/Controllers/VaultController.php:1317-1357` vs `1465-1477`

**Approach:** Extracted `guardMigrationTargetRelayUrl` helper; called from both `migrationStart` and `migrationCommit`. `javascript:`/`data:`/`file://` and malformed URLs now refused at start.

`migrationStart` writes `target` directly via `Validators::requireNonEmptyString`. `migrationVerifySource` re-emits this URL to admin callers. `migrationCommit` has `filter_var(FILTER_VALIDATE_URL)` + scheme check. So an admin can persist `javascript:`, `data:`, `file://`, or internal-only URL at start; the desktop's "switch active relay" path may follow it before commit catches the bad URL.

**Fix:** extract the URL validator into a helper, call from `migrationStart` too.

#### ~~§1.H4~~ — `POST /api/vaults` (create) skips `guardFormatVersion` / envelope-prefix consistency checks
**Fix landed:** d5222cd 2026-05-17
**File:** `server/src/Controllers/VaultController.php:134-141`

**Approach:** Parse both envelopes via `VaultCrypto::parse*EnvelopeHeader`; call `guardFormatVersion`; assert envelope vault_id + revision match the request body. Same shape as `putHeader` / `putRoot`.

`createVault` decodes `encrypted_header` + `initial_root_ciphertext` and stores them via repositories without running `VaultCrypto::parseHeaderEnvelopeHeader` / `parseRootEnvelopeHeader`. `putHeader` and `putRoot` run `guardFormatVersion` to enforce 0x01 before AEAD attempt. The asymmetry means a malformed envelope can be persisted at create-time.

**Fix:** call parse helpers + `guardFormatVersion` on `createVault`; confirm envelope `vault_id` + revision values match request body fields.

#### ~~§1.H5~~ — `gcCancel` permits cross-author cancellation by any `sync`-role caller
**Fix landed:** a28f46d 2026-05-17
**File:** `server/src/Controllers/VaultController.php:1252-1295`

**Approach:** Sync-role cancel now also requires either ownership-match (`requested_by_device_id == caller`) or admin role. Cross-author cancel by a non-admin sync caller now 403s.

`gcCancel` cancels any open job regardless of `requested_by_device_id`. The repo records `requested_by_device_id` at row creation (line 1091) but the controller never consults it. A compromised paired device with `sync` role can interfere with the legitimate admin device's GC, eventually exhausting quota.

**Fix:** require admin OR ownership-match: `if ($job['requested_by_device_id'] !== $callerDevice && $callerRole !== 'admin') throw …;`.

#### ~~§1.H6~~ — Chunk-write failure path's row-delete + bytes-decrement is non-transactional
**Fix landed:** 380dc2b 2026-05-17
**File:** `server/src/Controllers/VaultController.php:884-887`

**Approach:** Wrap `deleteRow` + `incUsedBytes(-size, -1)` in BEGIN IMMEDIATE / COMMIT so a crash between them either commits both or rolls back both — no permanent (used_bytes, chunk_count) skew.

After `COMMIT` lands and the disk write fails, two writes (`deleteRow` + `incUsedBytes(-size, -1)`) happen sequentially with no transaction. Crash between → permanent counter skew.

**Fix:** wrap both in `BEGIN IMMEDIATE`/`COMMIT`.

### Medium

#### ~~§1.M1~~ — Hash fields (`header_hash`, `root_hash`, `shard_hash`) not validated as `^[a-f0-9]{64}$`
**Fix landed:** 15ee4ca 2026-05-17
**File:** `VaultController.php` (multiple sites: 136, 273, 396, 494, 585, 1029)

`vaultRequireNonEmptyString` accepts any non-empty string. A client could store `"banana"` as a `shard_hash`. The §10.C reader check would catch it on decrypt, but the relay returns `current_root_hash: "banana"` in 409 payloads.

**Approach:** New private static `vaultRequireHex64` validates `^[a-f0-9]{64}$` and emits 400 `vault_invalid_request` with field attribution. Applied across all six write sites.

#### ~~§1.M2~~ — Server-side AAD builders inconsistent on `strlen(canonical) === 12` assertion
**Fix landed:** 2e466fe 2026-05-17
**File:** `server/src/Crypto/VaultCrypto.php`

`buildRootAad` (340) and `buildShardAad` (387) assert canonical 12-byte vault_id length. `buildChunkAad`, `buildHeaderAad`, `buildRecoveryAad`, `buildDeviceGrantAad` do NOT. Python asserts in *every* builder. Future controller that forgets `normalizeVaultId` silently produces a wrong-length AAD that still passes AEAD on this device but breaks cross-runtime parity.

**Approach:** Added the same `strlen(canonical) === 12` assertion in the four missing builders. Four new `test_build*Aad_rejects_malformed_vault_id` pins in `VaultCryptoInvariantsTest.php`.

#### ~~§1.M3~~ — `gcExecute` role check sequenced after state inspection — minor info leak
**Fix landed:** 10fce54 2026-05-17
**File:** `VaultController.php:1118-1184`. `getById($planId)` and state checks run before `requireRole(sync)`. A `read-only` caller can probe plan IDs and learn state from error codes. Low impact (random IDs, 2^120 entropy). Move role check to top of method.

**Approach:** Hoisted `requireRole(sync)` to the top of `gcExecute` (before plan lookup). Below-sync callers get a uniform 403 before any state inspection. KIND_SCHEDULED_PURGE / KIND_FORCED_EVICTION still escalate to admin afterward; the duplicate inner `else { requireRole(sync) }` is removed.

#### ~~§1.M4~~ — Router catches only `ApiError` — uncaught `\Throwable` leaks PHP error envelope
**Fix landed:** 2ccafd4 2026-05-17
**File:** `server/src/Router.php:140`. Deploy doc doesn't mandate `display_errors=Off`. Add `catch (\Throwable $e)` arm; log + emit `503 vault_storage_unavailable` or `500 vault_internal_error` with no details.

**Approach:** Added `catch (\Throwable $e)` arm below the existing `ApiError` branch. Full trace logged via new `apierror.uncaught_throwable` event server-side; wire envelope is `500 vault_internal_error` with no details. Two new RouterErrorHandlingTest pins: uncaught Throwable → typed envelope (private message not leaked); thrown ApiError still flows through ErrorResponder unchanged.

#### ~~§1.M5~~ — `recordRotation` audit-row INSERT non-atomic with `rotateAccessTokenHash`
**Fix landed:** d91ea3a 2026-05-17
**File:** `VaultGrantsController.php:541-548`. Crash between leaves rotation done but no audit row. Wrap in IMMEDIATE tx. Also: `rotateAccessTokenHash` doesn't return its result to the controller — if `changes() !== 1` (e.g. vault disappeared mid-request), controller still returns success.

**Approach:** Wrapped the rotate+record pair in BEGIN IMMEDIATE/COMMIT with ROLLBACK on any throw. ``rotateAccessTokenHash``'s ``bool`` return is now asserted — a false (vault row missing) raises ``VaultNotFoundError`` which unwinds the transaction.

#### ~~§1.M6~~ — `decodeBase64Field` accepts `"=="` when length not constrained
**Fix landed:** 6e5df0c 2026-05-17
**Files:** `VaultController.php:106`, `VaultGrantsController.php:60`. Un-length-checked sites accept empty-payload base64. An approver could store an empty wrapped grant; claimant unwraps nothing.

**Approach:** Defense-in-depth — PHP's strict-mode `base64_decode` already rejects pure-padding `"=="` transitively, but explicit `strlen($raw) === 0` guard added to both helpers so a future non-strict refactor doesn't silently widen the surface. Source-pin test asserts both helpers contain the marker.

#### ~~§1.M7~~ — `migrationVerifySource` unconditionally stamps `verified_at` even when vault is `migrated_to`
**Fix landed:** 86a7087 2026-05-17
**File:** `VaultController.php:1404`. Behaviour relies on `COALESCE` keeping original timestamp — subtle, would benefit from an explicit `state` guard.

**Approach:** Added explicit `$alreadyCommitted = $vault['migrated_to'] !== null` guard before `markVerified`. Pre-fix the COALESCE preserved any prior timestamp; the invariant "verified_at is only ever stamped pre-commit" is now readable in one place. Test simulates the pathological state (verified_at scrubbed post-commit) and asserts /verify-source doesn't re-stamp.

### Low

#### §1.L1 — `Validators::requireNonEmptyString` rejects `"0"` via `empty()` semantics
**File:** `server/src/Http/Validators.php:18-24`. Cosmetic.

#### §1.L2 — `migrationCommit` URL parser accepts private IPs / localhost
**File:** `VaultController.php:1465-1477`. Worth optional blocking-by-default with operator override.

#### §1.L3 — `VaultStorage::ensureDir` chmod 0700 inaccessible to other request pools on shared-host PHP-FPM
**File:** `server/src/VaultStorage.php:45`. Operator-deployment caveat.

#### §1.L4 — `getJoinRequest` row LOOKUP not vault-scoped (defense-in-depth gap)
**File:** `VaultGrantsController.php:191-217`. The row's `vault_id` mismatch is caught post-fetch; `$repo->get($reqId, $vaultId)` would be tighter.

### Info (verified clean)

- **Path traversal in chunk storage:** `VaultChunksRepository::storagePath()` validates `^[A-Z2-7]{12}$` and `^ch_v1_[a-z2-7]{24}$` at the bottom of the call chain — defense-in-depth.
- **`shard-with-root` atomicity:** `tryAtomicShardWithRootCAS` uses one IMMEDIATE tx, double `changes()===1` check, single COMMIT — race-safe.
- **Per-endpoint role gate coverage** on manifest/header/grants/migration endpoints is **complete** (modulo §1.H5 ownership). The team's §3.3 eval mitigation claiming "role gates partial" is wrong here — they're complete on those endpoints. The real role-gate gap is **§3.C1: eviction stages 2-3**.
- **Join-request claim atomicity:** `UPDATE … WHERE state='pending'` + `changes()===1` — race-safe.
- **GC plan TTL replay:** `markCompleted` filters `state IN (planned, executing)`.
- **SQL injection:** all queries use named parameters.
- **Migration schema:** migrations 002–005 form a coherent set with proper FK + indexes. The agent's earlier claim that `vault_access_secret_rotations` was missing turned out to be **false** — it's in 004 (verified personally).

---

## §2. Desktop crypto + manifest model

The byte-level module is **well-built**: explicit length assertions on every AAD field, byte-exact constructions, format-version guard before AEAD attempt, NFC-normalised passphrases, CSPRNG nonces, Argon2id parameters locked (m=131072, t=4, p=1). All 8 AAD builders produce the spec-locked byte lengths. Cross-runtime test vectors pass byte-exactly.

### High

#### ~~§2.H1~~ — Manifest revision floor: cached state overwritten *before* rollback check fires
**Fix landed:** dbd11a9 2026-05-17
**File:** `desktop/src/vault/vault.py:538-549`

**Approach:** Hoist `_verify_root_floor_or_raise` above the four cache writes so a rollback raises before any state mutation. Matches the legacy decrypt_manifest path's ordering.

```python
root = self.decrypt_root_envelope(envelope_bytes)
self._root_envelope = envelope_bytes        # ← side effect FIRST
self._root_revision = int(root["root_revision"])
self._manifest_ciphertext = envelope_bytes
self._manifest_revision = self._root_revision
if local_index is not None:
    self._verify_root_floor_or_raise(root, local_index)   # ← check AFTER
```

If the relay serves a downgrade, the floor check raises but the **in-memory cache is already clobbered with the stale state**. Subsequent reads see the rolled-back values until the next successful fetch. Spec §3.7 / `relay_errors.py:120`: "the local folder cache stays at the last-good revision" — the *legacy* path at line 988 does this correctly (check, then refresh); the new sharded path doesn't.

**Fix:** hoist the floor check *before* the four attribute writes.

#### ~~§2.H2~~ — `binding/sync.py:_publish_batch_with_cas_retry` uses blind path-append on CAS retry
**Fix landed:** e1fe2f9 2026-05-17
**File:** `desktop/src/vault/binding/sync.py:1106, 926-989`

**Approach:** Mirror commit 3fb7470's folder-upload fix — first attempt blind apply, flip `use_merge=True` on the first 409, rebuild candidate via new `_merge_batch_into_shard_with_bump` that handles both uploads (via `merge_local_version_into_shard` for §D4 collision-rename + tie-break) and deletes.

Recent commit `3fb7470 fix(vault): folder-batch CAS retry must run §D4 merge on conflict` fixed the same bug in `upload/folder.py` (flips `use_merge=True` after first 409). The backup-sync engine's `_publish_batch_with_cas_retry` was NOT fixed — still re-invokes `_apply_batch_to_shard` (blind path match) on every retry. Two devices uploading the same path to a backup-only folder under different `entry_id`s → Device B's version silently appended to Device A's entry, losing the §D4 collision-rename. Comment claims "last-writer-wins on backup-only" but the manifest result is incoherent (entry_id is supposed to be path-stable per device).

**Fix:** match the upload-path fix (use_merge after first 409) or rigorously document the backup-only collapse semantics.

#### ~~§2.H3~~ — Sensitive-op fresh-unlock window is 120 s, architecture doc implies 15 min
**Fix landed:** b1d2cde 2026-05-17
**File:** `desktop/src/vault/fresh_unlock.py:36`, `docs/vault-architecture.md:1020`

**Approach:** `FRESH_UNLOCK_WINDOW_S = 900.0` (15 min). Per-process — process restart re-locks.

`FRESH_UNLOCK_WINDOW_S = 120.0`. Spec §13 says "15 min idle...sensitive ops always require fresh unlock regardless". Chained ops (revoke → rotate access secret) past 2 min re-prompt twice. Either bump to match the doc or update §13.

### Medium

#### §2.M1 — `decrypt_root_envelope` / `decrypt_shard_envelope` skip plaintext-prefix vault_id sanity check
**File:** `desktop/src/vault/vault.py:552-584, 670-708`

CAS-conflict-payload decrypt paths build AAD from envelope-extracted fields but use `self._vault_id` for AAD. AEAD catches a vault_id mismatch (CryptoError), but the error becomes "vault_root_tampered" rather than "wrong vault_id in envelope". The header path does this explicitly (line 482-483); root + shard paths don't. Cheap defense-in-depth.

#### §2.M2 — `VaultFormatVersionUnsupported` typed exception is defined but never raised
**File:** `desktop/src/vault/crypto.py:95-142`

Production decrypt paths re-implement the byte check inline with bare `ValueError` containing the error code in the message string. Callers can't `except VaultFormatVersionUnsupported`; they have to substring-grep. Migrate the 4 inline checks to call `assert_supported_format_version(envelope_bytes, kind="root")`.

#### §2.M3 — Recovery envelope wire form per §12.4 is build-only — no production read path
**File:** `desktop/src/vault/crypto.py:852-881`

The 131-byte envelope is only built by test vectors. Production stores discrete fields (`argon_salt`, `nonce`, `aead_ciphertext_and_tag`, `argon_params`) in JSON inside the header. The actual unwrap path bypasses the format-version byte. Either drop §12.4 from the spec or migrate the open path to consume byte-form envelopes.

#### §2.M4 — Legacy `dc-vault-v1/manifest` HKDF label still reachable in `browser_model.decrypt_manifest`
**File:** `desktop/src/vault/ui/browser_model.py:100-108`

Verified personally. Recent commit `075e3ff` claims the legacy surface was dropped, but `browser_model` still falls back to `derive_subkey("dc-vault-v1/manifest", ...)` + `build_manifest_aad(...)` on AEAD failure of the root path. The label isn't in spec §4.2. Dead-compat path; maintenance trap.

**Fix:** remove the legacy fallback.

#### §2.M5 — PHP server NFC normalises only when `intl` extension is installed
**File:** `server/src/Crypto/VaultCrypto.php:177-183`

Inert today (server never derives passphrase keys), but a future server-side flow with non-ASCII passphrase on a host without `php-intl` derives a different key than the Python client. Either remove `argon2idKdf` from the PHP surface or hard-fail when intl is missing.

### Low

#### §2.L1 — Genesis fingerprint compared with `!=`, not `hmac.compare_digest`
**File:** `desktop/src/vault/import_/bundle.py:146-150`. Defense-in-depth pattern.

#### §2.L2 — CAS retry loop has no overall livelock cap beyond `CAS_MAX_RETRIES`
**File:** `desktop/src/vault/binding/sync.py:1058`. Under 10-device write storms a single device could exhaust budget per-folder forever. Add 50-100 ms × attempt backoff for log-volume sanity.

#### §2.L3 — `_imported_rename` caps at 10 000 collisions with `RuntimeError`
**File:** `desktop/src/vault/manifest.py:861-866`. Acceptable sanity bound; flag for awareness.

#### §2.L4 — `verify_recovery_kit` catches bare `Exception` → masks real OS errors as wrong-passphrase
**File:** `desktop/src/vault/recovery_kit.py:268-271`. Should let `KeyError` etc. bubble; only `nacl.exceptions.CryptoError` is "wrong passphrase".

### Info — verified clean

- **Format-version byte is plaintext, not in AAD** — spec-conformant. v2 envelope can be refused before deriving keys.
- **Argon2id parameters locked exactly per spec** — m=131072 KiB, t=4, p=1, out=32, salt=16. Per-envelope params persisted in kit + header.
- **Chunk-id namespace gate via `ch_v1_` prefix** is correctly enforced.
- **Deterministic chunk nonce binds plaintext + version_id + chunk_index** — stronger than spec's "(version, index)"-only formula; spec promise still holds.
- **CAS retry loop fixed to N attempts, not N+1** — commit `d428521` correctly restructured all 6 helpers.
- **Constant-time tag verification via libsodium** — Python + PHP both libsodium-backed.
- **`archived_op_segments = []` always emitted** — segment envelopes deferred to v1.5 per README.

---

## §3. Desktop sync engine — binding, watcher, twoway, baseline, preflight

This is where most of the reliability and data-loss bugs live.

### Critical

#### ~~§3.C1~~ — Eviction stages 2–3 hard-purge with `sync` role only; no `purge_secret` gate
**Fix landed (partial — admin gate only):** f621dc1 2026-05-17 — `purge_secret` follow-up in `review-doubts.md`.
**Files:** `desktop/src/vault/ops/eviction.py:312-348`, server `VaultController.php:1087, 1182-1184`

**Approach:** New `KIND_FORCED_EVICTION` plan kind + `purpose='forced_eviction'` body param on `gc/plan`. Stages 2/3 send the new purpose; server enforces admin role on plan creation, execute, and cancel. Stage 1 stays sync. `purge_secret`/passphrase-prompt UI deferred to a scoped follow-up.

Stages 2 (unexpired tombstones early-purge) and 3 (historical version purge) call `relay.gc_plan(..., kind=KIND_SYNC_PLAN)` and `relay.gc_execute(plan_id=…)` **without `purge_secret`**. Server gates `KIND_SYNC_PLAN` on `requireRole('sync')` only. Spec §12 destructive-action ledger requires scheduled hard-purge to need **admin + `purge_secret` + fresh unlock**. Eviction stages 2 and 3 *are* hard-purges — both irreversible deletions with no admin gate.

A compromised `sync`-role device can call `eviction_pass(target_bytes_to_free=large)` and destroy every tombstoned file inside its retention window plus every historical version. The user's "30-day deletion grace" promise (§D2) is violated.

**Fix:** split `KIND_SYNC_PLAN` so stages 2/3 require `admin` + `purge_secret`. OR document explicit acceptance in ADR + update §12.

#### ~~§3.C2~~ — Ransomware detector trip does NOT cancel an in-flight sync cycle
**Fix landed:** 3988c06 2026-05-17
**File:** `desktop/src/vault/binding/runtime_watchers.py:175-181`

**Approach:** Add `cancellation_registry` field to `VaultWatcherRuntime` and pass to `pause_binding(..., cancellation=registry)` on trip. Tray creates one registry and shares it with both the watcher runtime and the autosync's `should_continue` closure (which now consults the per-binding event in addition to global quit).

`_on_tripped` calls `pause_binding(self.store, binding_id)` with **no `cancellation=`**. Cancellation registry exists (`vault_folders/tab.py:73`), `pause_binding` accepts it — the watcher runtime just doesn't pass it. Effect: trip pauses binding **in DB only**. Any concurrent worker already inside `run_backup_only_cycle` / `run_two_way_cycle` keeps draining ops + CAS-publishing tombstones until current batch finishes (~50 tombstones).

For a deletion-attack the detector blocks future cycles only. The current cycle still bleeds.

**Fix:** thread the existing `BindingCancellationRegistry` into `VaultWatcherRuntime` and call `pause_binding(..., cancellation=registry)` + optionally `cancellation.cancel(binding_id)` *before* the DB flip.

#### ~~§3.C3~~ — `scan.py` + `preflight.py` follow symlinks → exfiltrate files outside the binding root
**Fix landed:** 92c5fed 2026-05-17
**Files:** `desktop/src/vault/binding/scan.py:60-72`, `desktop/src/vault/binding/preflight.py:212-217`

**Approach:** Replace `stat()` with `lstat()` in both walk loops + `S_ISREG` guard; non-regular leaves emit `vault.sync.special_file_skipped` per existing diagnostics catalog. Mirrors baseline.py's existing behaviour.

Verified personally. `absolute.stat()` follows symlinks. `_is_regular_file(stat)` sees a regular file at the *symlink target*. The path is enqueued for upload with the symlink's binding-relative name. `baseline.py:263+` correctly uses `os.lstat()`.

Exfiltration: attacker creates `~/Documents/note.txt -> /etc/shadow` in a Documents-bound vault. Scan walks Documents → stats `note.txt` → sees regular file → queues upload → uploader opens by relative path → reads `/etc/shadow` content. Vault stores `/etc/shadow` ciphertext under `note.txt`. Later delete of the symlink tombstones what looks like a normal file on the relay.

**Fix:** replace `path.stat()` with `path.lstat()` + `S_ISREG` in `scan.py:60` and `preflight.py:213`. Emit `vault.sync.special_file_skipped`.

### High

#### ~~§3.H1~~ — Detector records AFTER watcher enqueues delete op
**Fix landed:** e26cf1b 2026-05-17
**File:** `desktop/src/vault/binding/filesystem_watcher.py:212-218`, `runtime_watchers.py:137`

**Approach:** Re-order `observe_with_detector` so `detector.record` runs first, then check `verdict.tripped` and early-return without forwarding (so the trip-causing event itself doesn't enter the pending-ops queue).

`_enqueue_delete_if_synced` immediately `store.coalesce_op`s the delete. The detector record happens *after* the wrapped `observe` returns. Sequence for the 200th malicious delete: enqueue tombstone #200 → detector trips → pause binding. Op #200 is already in the queue.

**Fix:** re-order — `detector.record` → check tripped → if tripped, do NOT call `wrapped_observe`. Or gate `coalesce_op` on the detector verdict.

#### ~~§3.H2~~ — `download_folder` is uncancellable; `_get_chunk_with_retry` can block 4 × 60 s × N chunks
**Fix landed:** 7bf3df4 2026-05-17
**File:** `desktop/src/vault/download/folder.py:46-123`

**Approach:** Thread `should_continue` through `download_folder` and the per-chunk retry. New diagnostics event `vault.download.folder_cancelled`.

#### ~~§3.H3~~ — `download_latest_file` / `download_version` buffer entire plaintext in RAM
**Fix landed:** 11f0e69 2026-05-17
**File:** `desktop/src/vault/download/single_file.py:97, 135, 147, 209, 245, 259`

**Approach:** Generator-driven `atomic_write_chunks` in both single-file paths; peak RAM drops from `2 × file_size` to `~1 chunk`.

#### ~~§3.H4~~ — Two-way `trash_path` falls back to `unlink()` when `gio` missing → silent data loss
**Fix landed:** c98e913 2026-05-17
**File:** `desktop/src/vault/binding/twoway.py:586`, `ops/trash.py:67-75`

**Approach:** Pass `allow_unlink_fallback=False` from twoway's tombstone branch. Missing `gio` now marks the op `trash_failed` (visible to the user) and leaves the file in place rather than silently unlinking it.

#### ~~§3.H5~~ — `WatcherCoordinator._pending` and `_debouncer._last_seen` unlocked between watchdog and tick threads
**Fix landed:** 095523b 2026-05-17
**File:** `desktop/src/vault/binding/filesystem_watcher.py:191`

**Approach:** Single coarse `threading.Lock` on `WatcherCoordinator` covers all four mutable dicts (pending, debouncer, gate, snapshots). 8-thread × 200-iter smoke test asserts no deadlock + no exception during concurrent observe/tick.

#### ~~§3.H6~~ — Preflight uses stale manifest snapshot; baseline fetches fresh — diff staleness window
**Fix landed:** 638cf3d 2026-05-17
**File:** `desktop/src/vault_folders/dialog_connect_local.py:89` → `folder/runtime.py:241`

**Approach:** Thread preflight `revision` through `on_confirmed` → `run_initial_baseline(expected_root_revision=…)`. New typed `VaultBaselineHeadMovedError` raised when fresh fetch's revision differs; dialog catches it and asks the user to re-preflight.

Dialog opens preflight → snapshot manifest → user clicks Confirm → baseline starts → fetches fresh manifest. User's preflight expectation can be wrong.

**Fix:** pass the preflight manifest into `run_initial_baseline` and require the head not to have moved; force re-preflight on advance.

#### ~~§3.H7~~ — Two-way `_unique_conflict_path` TOCTOU between `exists()` and `shutil.move`
**Fix landed:** 7838450 2026-05-17
**File:** `desktop/src/vault/binding/twoway.py:866-890, 689`

**Approach:** New `_atomic_reserve_path` helper using `os.open(O_CREAT|O_EXCL|O_WRONLY)`. Replaces all three `exists()`-then-return sites in `_unique_conflict_path`. Returned path now exists as a 0-byte sentinel; the caller's `shutil.move` atomically overwrites it.

Loops `if not (local_root / candidate).exists(): return candidate`. Caller does `shutil.move(target, conflict_target)` without `O_EXCL`. Concurrent local create silently overwrites.

**Fix:** `os.rename` into an `O_EXCL`-opened sentinel, retry on `EEXIST`.

#### ~~§3.H8~~ — SQLite local index without WAL mode → watcher/sync contention serializes
**Fix landed:** f92b664 2026-05-17
**File:** `desktop/src/vault/state/local_index.py:246-251`, `vault/binding/bindings.py:561-565`

**Approach:** `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=5000` in both `_connect` implementations.

No `PRAGMA journal_mode=WAL`. Watcher writes block sync reads during a burst; the 3-second stability gate is short enough that mid-burst events can be queued with stale stat data.

**Fix:** `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000` in `_ensure_schema`.

#### ~~§3.H9~~ — `_apply_remote_to_local` ghost-row reaper trusts an empty `entries` list
**Fix landed:** 7b276bd 2026-05-17
**File:** `desktop/src/vault/binding/twoway.py:491-523`

**Approach:** Refuse to enter the ghost-reaper loop unless `state.shard.schema == "dc-vault-shard-v1"`. New `vault.sync.twoway_shard_schema_unexpected` event documented.

If the head shard is intermittently corrupt and returns `entries=[]`, every local-entries row appears orphaned → every on-disk file demoted to "extra" → next watcher tick re-uploads them all as new bytes. Self-DDoS.

**Fix:** sanity-check shard plaintext has `schema == "dc-vault-shard-v1"` and non-empty content before reaping.

### Medium

#### §3.M1 — Baseline doesn't validate binding state before running
**File:** `desktop/src/vault/binding/baseline.py:82`. No `assert binding.state == "needs-preflight"`. A second call on `bound` binding can overwrite local entries.

#### §3.M2 — Conflict-naming exhaust path leaks into `download_latest_file(existing_policy="overwrite")`
**File:** `desktop/src/vault/binding/twoway.py:866-895, 731`. `RuntimeError` from `_unique_conflict_path` isn't caught in `_apply_remote_upsert`; before the abort, the fallback to `existing_policy="overwrite"` may run.

#### §3.M3 — Stability gate timeout silently drops growing files (log not user-visible)
**File:** `desktop/src/vault/binding/filesystem_watcher.py:272-280`. 5-min cap → path dropped from `_pending` with log line only. File stays un-synced for hours.

#### §3.M4 — `prepare_upload_for_batch` reads bytes at prep time, not enqueue time
**File:** `desktop/src/vault/upload/single_file.py:486-493`. Last-write-wins, but breaks intuition for 200-event bursts.

#### §3.M5 — `disconnect_binding` drops pending ops; user's unsynced changes silently lost
**File:** `desktop/src/vault/binding/lifecycle.py:296-309`. Refuse disconnect with pending ops, OR run one final cycle synchronously.

#### §3.M6 — `download_folder` skips `fsync_dir` on the destination root after per-file loop
**File:** `desktop/src/vault/download/folder.py:80-123`. Power loss during 1000-file folder restore can lose some directory entries.

### Low

- **§3.L1** — `BatchedUploadStub` orphans reaped only on disconnect, not pause. (`lifecycle.py:319-341`)
- **§3.L2** — Ignore-dotfiles flag inconsistent across `scan.py` / `preflight.py` / `baseline.py`.
- **§3.L3** — Conflict-path random token is 32 bits; spec-compliant.
- **§3.L4** — `MAX_OP_ATTEMPTS=10` ops sit in the queue forever; no permanent-failure UI surface.

---

## §4. Desktop upload / download / eviction / delete / restore

### High

#### ~~§4.H1~~ — Upload session marked `complete` before disk unlink can leak permanent JSON files
**Fix landed:** 9aecb6d 2026-05-17
**File:** `desktop/src/vault/upload/single_file.py:346-348`, `vault/upload/session.py:119`

`session.phase = "complete"; save_session(...); clear_session(...)`. Crash between → JSON file persists with `phase == "complete"`. `list_resumable_sessions` filters it (correct), but no TTL reaper for top-level session files.

**Approach:** Both ends of the suggested fix at once: (1) inverted the order in `single_file.upload_file` — `clear_session` first, `save(complete)` fallback only if unlink raises; (2) added `reap_expired_sessions` mirroring `reap_expired_stubs` (14-day window, sweeps corrupt JSON + missing `created_at` too, ignores sub-dirs), wired into the same `runtime_watchers` boot site so both reapers run together. New events `vault.sync.session_ttl_reaped`, `vault.upload.session_clear_failed`, `vault.upload.session_tombstone_failed` catalogued.

#### ~~§4.H2~~ — Resume `complete_pending_publish` doesn't verify the relay vault is *yours*
**Fix landed:** a0859e3 2026-05-17
**File:** `desktop/src/vault/resume.py:230-272`

`_probe_relay_state` calls `relay.get_header(vault_id, vault_access_secret)`. No check that the existing relay row's `genesis_fingerprint` matches the master key the local grant decodes. Scenario: user runs wizard on relay A (orphan id created), repoints config to relay B where vault_id collides → resume writes new recovery envelope onto the unrelated vault under local master key. User sees recovery succeed; reality is double-corruption.

**Approach:** `_probe_relay_state` now requires the `master_key` and decrypts the header envelope inline (mirrors `Vault.fetch_header_plaintext`). AEAD-tag failure (bytes encrypted under a stranger's master key) raises a new typed `VaultIdentityMismatchError`. Defense-in-depth: even on decrypt success the embedded `genesis_fingerprint` must equal `_genesis_fingerprint_hex(master_key)` — guards against future header-format changes that might decouple the AEAD key derivation from the genesis anchor. Typed error so GTK can route to "Discard" rather than the generic "Retry" prompt. Existing tests updated to seed real header envelopes via new `_build_real_header_envelope` helper.

#### ~~§4.H3~~ — `clear_vault` doesn't reload root after each per-folder publish
**Fix landed:** 25470a3 2026-05-17
**File:** `desktop/src/vault/ops/clear.py:99-121`

Reads root once, iterates folders. Concurrent device adding a folder mid-clear → that folder isn't tombstoned. Audit event `vault.vault.cleared total_tombstoned=N` excludes it.

**Approach:** Replaced the single up-front fetch with a `for pass_index in range(8)` loop that re-fetches root each pass, walks only folders not in a `seen_folders` set, and exits early once a pass yields no new folders. 8-pass defensive cap emits `vault.vault.clear_pass_cap_hit` (added to diagnostics catalog) so an operator can spot a malicious device racing the clear with folder creates. Audit event now reports both `total_tombstoned` and `folders`.

#### ~~§4.H4~~ — Mid-stage eviction crash recovery silently bumps revisions for cleanup-only work
**Fix landed:** caa220e 2026-05-17
**File:** `desktop/src/vault/ops/eviction.py:330-391`. Cleanup-only branch returns `bytes_freed=0`; stage 1 re-fires looking for *more* freedom, eventually falls through to stage 4 (`no_more_candidates`). Also logs `vault.eviction.shard_cleanup_only` which isn't in the diagnostics catalog.

**Approach:** Cleanup-only stage 1 now emits a cascade-to-force warning; the missing `vault.eviction.shard_cleanup_only` event added to diagnostics catalog so the audit trail is complete.

#### ~~§4.H5~~ — Resume's seek-past-chunk uses `chunk_size`, not stored `plaintext_size`
**Fix landed:** 64a3e78 2026-05-17
**File:** `desktop/src/vault/upload/resume.py:118, 129`. Overshoots on last-chunk recovery. Currently raises (not silent corruption), but should use `int(record["plaintext_size"])`.

**Approach:** Seek now uses `int(record["plaintext_size"])` from the per-record dict (not `session.chunk_size`). Last-chunk recovery on non-multiple-of-CHUNK_SIZE files works correctly.

### Medium

#### §4.M1 — Folder upload CAS-exhaust leaves orphan active chunks the next eviction pass can't reclaim
**File:** `desktop/src/vault/upload/folder.py:436-519`. Comment claims orphans cleaned up; eviction stage 1 only purges chunks behind expired tombstones. Active orphan chunks have no reclamation path.

**Fix:** stage-0 orphan-chunk reaper (`batch-head` × manifest references; delete difference).

#### §4.M2 — `restore_remote_folder` symlink-escape check after mkdir(parents=True)
**File:** `desktop/src/vault/ops/restore.py:155-174`. Pre-existing symlink as parent dir → mkdir creates dirs through symlink target before escape check fires.

#### §4.M3 — `clear_vault` audit event missing on mid-loop crash
**File:** `desktop/src/vault/ops/clear.py:107-121`. Emit `vault.vault.clear_started` at top of loop.

#### §4.M4 — `purge_schedule` allows `delay_seconds=0`
**File:** `desktop/src/vault/ops/purge_schedule.py:126`. 0-delay purge fires within seconds. Minimum 1 hour, or separate confirmation for sub-hour delays.

### Info

- `integrity.py` correctly avoids auto-repair-by-deletion.
- Purge schedule default 24h grace + cancel window match spec.
- Chunk dedup is per-(file_version_id, chunk_index), NOT per-byte-stream. Cross-file dedup intentionally absent.

---

## §5. Recovery kit, grants, QR-join, export, import, migration

### Critical

#### §5.C1 — Full migration wizard does not exist; runner has no production driver — *skipped (new feature build)*
**Status:** logged in `docs/plans/review-doubts.md` §5.C1 — needs explicit scoping; never build a new feature autonomously.
**Files:** `desktop/src/windows_vault/tab_migration.py:54-63` (button disabled), `desktop/src/vault/migration/runner.py` (library)

The "Migrate to another relay…" button is permanently `set_sensitive(False)`. Repo-wide grep for `run_migration` (non-test) → **zero hits**. The §H2 state machine, target bootstrap, verification, and `on_committed` callback exist as well-tested library code that nothing in production calls. The architecture doc and spec list migration as v1; the UI ships zero-of-it.

#### §5.C2 — QR-join + grant approval flows are library-only; no UI exists — *skipped (new feature build)*
**Status:** logged in `docs/plans/review-doubts.md` §5.C2 — memory note `project_vault_multi_device_story.md` already records that v1 ships recovery-kit-only.
**Files:** `desktop/src/vault/grant/qr.py`, `vault/grant/wrap.py`, but `make_join_url`, `wrap_grant_for_claimant`, `unwrap_grant_for_claimant`, `parse_join_url` all have **zero non-test callers** in `src/`.

No admin "approve a join request" UI, no claimant "scan/paste join URL" UI, no orchestrator hitting `/join-requests/{req_id}/{claim,approve}`. The only viable multi-device path on desktop today is recovery-kit (passphrase + 32-byte secret) — exactly the path memory `project_vault_multi_device_story.md` describes. Architecture doc and protocol spec list QR-join as v1, but the desktop surface ships recovery-kit-only.

#### ~~§5.C3~~ — Multi-device migration propagation: no caller invokes `propagate_relay_migration`
**Fix landed:** 2627d9a 2026-05-17
**File:** `desktop/src/vault/migration/propagation.py:37`. Repo-wide grep returns zero non-test, non-self hits.

**Approach:** Invoke `propagate_relay_migration` inside `VaultHttpRelay.get_header` (the single choke point). When `migrated_to` is set, persist the decision to `Config` (`server_url`, `vault_previous_relay_url`, expires-at). Next vault-relay construction reads the new URL. Two new event tags added to the diagnostics catalog.

No path in `vault_binding_*.py`, `runtime.py`, or the manifest/header fetch helpers handles `migrated_to` on `GET /header`. A vault migrated by Device A becomes silently inaccessible to Devices B…N until manual config edit. The §H2 contract "other devices switch on next header fetch" is unimplemented on the *receiver* side.

**Fix:** wire `propagate_relay_migration(header_data, current_relay_url)` into the central `GET /header` reader (`vault/binding/runtime.py` or `vault/ui/browser_model.py`). On `should_switch=True`, write `server_url`, `vault_previous_relay_url`, `vault_previous_relay_expires_at` + surface a banner.

### High

#### ~~§5.H1~~ — Import wizard never plumbs `genesis_fingerprint` → identity gate collapses to vault_id-only
**Fix landed:** d6a2b11 2026-05-17
**File:** `desktop/src/windows_vault_import.py:265-266, 362-369`

**Approach:** Add `Vault.fetch_header_plaintext(relay)` for the active vault's fingerprint; persist `genesis_fingerprint` in the export bundle's `RECORD_TYPE_HEADER` plaintext (`BundleHeaderInfo.genesis_fingerprint`); wizard's open + run paths now extract both and pass them through `decide_import_action`. Legacy bundles missing the field fall back to vault_id-only.

Calls runner with `active_genesis_fingerprint=None, bundle_genesis_fingerprint=None`. `decide_import_action` (`vault/import_/bundle.py:146-150`) short-circuits the comparison when either side is `None`. Merge happens on vault_id match alone. The bundle header carries no `genesis_fingerprint` field; the active vault writes it into the *encrypted header envelope* which the wizard does not extract.

The documented invariant "vault_id AND genesis_fingerprint must both match for merge" is not enforced end-to-end. An attacker who could forge a vault_id collision bypasses the cryptographic anchor.

**Fix:** thread the active vault's decrypted-header `genesis_fingerprint` into the wizard; persist it in the export bundle's `RECORD_TYPE_HEADER` plaintext; require both at the gate.

#### §5.H2 — Per-folder import conflict resolution UI doesn't exist — *skipped (new feature build)*
**Status:** logged in `docs/plans/review-doubts.md` §5.H2 — library is ready (`find_conflict_batches`), but a new wizard page between Preview and Progress with N pickers + "Apply to remaining" buttons is ~300 LOC of new UI. Wizard defaults to `rename` (conservative, no data loss), so shipping behaviour stays safe.
**File:** `desktop/src/windows_vault_import.py:36, 364`

The wizard always passes `ImportMergeResolution(per_folder={})`. Module docstring admits "Conflict-resolution UI…is not yet wired here". Defaults to `rename` per `DEFAULT_CONFLICT_MODE` — conservative (no data loss). But the user **cannot pick** `overwrite` or `skip` per folder, contrary to spec §17's "per-folder conflict batches with 'Apply to remaining'".

**Fix:** wire `find_conflict_batches` into a wizard page between Preview and Progress.

#### §5.H3 — Access-secret rotation has no client trigger — *skipped (new feature build)*
**Status:** logged in `docs/plans/review-doubts.md` §5.H3 — library waits for callers; until rotation is wired, nothing breaks. Pre-emptive risk that requires a new tab UI + server endpoint + kit-regeneration prompt to address.
**File:** `desktop/src/vault/grant/access_rotation.py:65-110`. Library ships `generate_new_secret`, `rotation_request_body`, reminders. No production caller invokes them. Tooltip in `tab_recovery.py:56` says "Recovery-material rotation is not implemented yet".

When rotation lands, every existing recovery kit becomes silently undecryptable on the relay side (right master_key, wrong bearer). There is no kit-regeneration prompt; users will be stranded.

#### ~~§5.H4~~ — Bundle preview misleads on "chunks already on relay" — never updated
**Fix landed:** cbfe33f 2026-05-17
**File:** `desktop/src/windows_vault_import.py:267, 485`

Passes `chunks_already_on_relay=0` with comment "filled at run-time" — but the preview page shows it verbatim ("0 of N chunks already on this relay"). The real `batch_head_chunks` call happens inside `run_import` *after* the user clicks Import. User makes import decisions on bandwidth fantasy.

**Approach:** Added optional `relay: ImportRelay | None = None` to `open_bundle_for_preview`. When provided, the function calls `batch_head_chunks` inline and overrides the (now-default-0) `chunks_already_on_relay` parameter. Wizard wires the live relay; preview page now shows real numbers before commit. Round-trip test exports a bundle to relay A and verifies preview against A reports the full count, while preview against empty relay B reports zero.

### Medium

#### §5.M1 — `state["passphrase"]` lives in plaintext Python dict for entire import-wizard lifetime
**File:** `desktop/src/windows_vault_import.py:243`. Never zeroed. Python `str` is immutable (residual bytes pinned in the string-intern table). Severity raised if a future refactor reuses the wizard.

#### §5.M2 — Migration runner: `_bootstrap_target_and_inventory` may fail genesis-insert for shards with revision > 1
**File:** `desktop/src/vault/migration/runner.py:476-503`. Code comment admits: server's `putShard` rejects `new != expected + 1`; idempotent re-entry path requires `current_hash == shard_hash` (fails on "rejected at validation"). Combined with §5.C1 nobody hits this today, but the moment migration is wired, every non-trivial vault fails this stage.

#### §5.M3 — Fresh-unlock window is per-process; import subprocess gets its own
**File:** `desktop/src/vault/fresh_unlock.py:38-40`. User who just typed the passphrase in Settings re-types in the import wizard subprocess. Spec UX of "single confirm within 120 s" doesn't compose across subprocesses.

#### §5.M4 — Export bundle decrypt uses brute-force record-type loop
**File:** `desktop/src/vault/export/bundle.py:483-518`. Tries each of 6 record-type AADs in order. AEAD failures are constant-time, but wall-clock cost is 6×. Add a 1-byte record-type tag outside AEAD in the next format bump.

#### §5.M5 — Export passphrase has no minimum entropy/length check
**File:** `desktop/src/vault/export/bundle.py:148`. Can export with `"x"`. Architecture invariant "different from recovery passphrase by default" not enforced either.

#### §5.M6 — Migration record `previous_relay_url` overwrite-protection is over-cautious
**File:** `desktop/src/vault/migration/state.py:172-174`. A→B then B→C may carry stale `previous_relay_url=A` in B→C's record if state file survived.

### Info — verified clean

- Recovery kit format (§12.5) byte-correct: `# header comment` block + `key: value` lines, UTF-8 LF.
- Argon2id params persisted in kit so future cost bumps still recover old kits.
- Cold-restore master key path uses kit-embedded envelope (resolves the relay-side chicken/egg).
- Onboarding kit save flow (`windows_vault/onboard_window.py:566-639`) follows memory note `feedback_security_ux`: explicit file dialog, loss warning, opt-in "delete after close", gated Done button.
- Recovery test is **real** (Argon2id round-trip + AEAD-decrypt), not theatre — memory `feedback_no_fake_tests` satisfied.
- QR-join *protocol* implementation (when wired): payload contains only `{relay_url, vault_id, join_request_id, ephemeral_pubkey, expires}`. Verification code byte-correct. Grant AAD binds `claimant_device_id` (replay-proof).

---

## §6. GTK windows + UX guards

### Critical

#### ~~§6.C1~~ — Recovery Test runs Argon2id on the GTK main thread (UI freeze, perceived crash)
**Fix landed:** c080d99 2026-05-17
**File:** `desktop/src/windows_vault/tab_recovery.py:326-391`

**Approach:** Wrap `run_recovery_material_test` in a `threading.Thread` worker mirroring `fresh_unlock_prompt.py:213-245`; disable Test + Close while running; settle via `GLib.idle_add`.

Click "Test recovery" → synchronous `run_recovery_material_test` → `verify_recovery_kit` → Argon2id (1–10 s by spec). The onboarding wizard's `perform_create` correctly off-loads to a worker thread + spinner; this re-test path skipped the same treatment.

Users will believe the app crashed and force-kill it. Real verify that hangs is worse than no verify.

**Fix:** wrap in a worker thread matching `fresh_unlock_prompt.py:213-245`; spinner + disabled Test button; settle via `GLib.idle_add`.

#### ~~§6.C2~~ — Recovery-kit export verify runs Argon2id on the GTK main thread
**Fix landed:** c080d99 2026-05-17
**File:** `desktop/src/windows_vault/onboard_window.py:583-650`

**Approach:** Same worker shape as §6.C1 applied to the file-dialog callback. Export button disabled while Argon2id runs; verify status repainted on settle.

Onboarding's "Export and verify recovery kit" synchronously calls `verify_recovery_kit` from the file-dialog callback. Same hang — on the **mandatory Done-button-gating path** of a brand-new vault wizard. A perceived hang here drives users to close the window and lose fresh recovery material.

#### ~~§6.C3~~ — `on_retry_publish` blocks the GTK main thread on relay POST
**Fix landed:** c080d99 2026-05-17
**File:** `desktop/src/windows_vault/onboard_window.py:1105-1148`. Synchronous `vault.publish_initial(relay)` inside click handler. Flaky relay (the precise condition the user is retrying) freezes UI for full HTTP timeout.

**Approach:** Same worker shape applied. Retry button disabled during the POST; success/error status repainted on settle.

#### ~~§6.C4~~ — Vault Browser "Delete folder" hamburger bypasses Danger-zone guards
**Fix landed:** 800de43 2026-05-17
**Files:** `desktop/src/windows_vault_browser/delete_restore.py:151-192`, `panes.py:377-381`

**Approach:** Route `_confirm_delete_folder` through `require_fresh_unlock_or_prompt`, then open a confirm dialog with a `Gtk.Entry` typed-confirm gated by `confirm_folder_clear_text_matches`. Matches the Danger tab's clear-folder flow exactly.

Per-row "Delete folder" menu item presents only an `Adw.AlertDialog` ("Delete contents of X?") and on confirm publishes a sharded tombstone of every file via `delete_folder_contents`. **No typed-confirm, no fresh-unlock**.

This is the same destructive backend as Danger tab "Clear folder" (`vault.ops.clear.clear_folder` wraps `delete_folder_contents` with `path_prefix=""`), but the Danger tab requires typed display name + fresh-unlock + 15-min-style gate. Spec table at `docs/vault-architecture.md:1006` says clearing a folder needs typed-confirm + fresh-unlock unconditionally — the browser surface is a quiet bypass.

**Fix:** route `_confirm_delete_folder` through `require_fresh_unlock_or_prompt` + typed-confirm Entry seeded with the folder display name (reuse `confirm_folder_clear_text_matches`). Or restrict the hamburger to file-level deletes and force folder-level through Danger.

#### ~~§6.C5~~ — Schedule hard-purge has **no admin-role gate** (client OR server)
**Fix landed:** 6f85a0d 2026-05-17
**Files:** `desktop/src/windows_vault/tab_danger.py:454-541`, `desktop/src/vault/ops/purge_schedule.py:101-153`

**Approach:** Add `caller_role` to the relay's `GET /header` response (derived from `vault_device_grants`). Desktop reads it on Schedule purge click (worker thread, after fresh-unlock) and bails with a clear error if `role != admin`. Server-side execution gate at `/gc/execute` for `KIND_SCHEDULED_PURGE` was already in place.

Spec at `docs/vault-architecture.md:1008` requires "typed-confirm vault ID + delay + fresh unlock + `admin` role". Desktop only checks typed-confirm + fresh-unlock. No role check, no `is_admin_device` lookup, no relay-side authorization consulted. **Any device with the recovery passphrase can schedule a purge.** The threat model puts `admin` between "lost laptop with cached passphrase" and "permanently destroyed vault". Missing.

**Fix:** surface device role from grant metadata; gate Schedule-purge button behind `set_sensitive(role == "admin")`. Verify server-side enforcement on `gc/execute`.

### High

#### ~~§6.H1~~ — Hard-purge scheduling is client-side-only with no executor
**Fix landed (partial — detection + notification):** 0b836aa 2026-05-17 — auto-execute design tracked in `review-doubts.md`.
**File:** `desktop/src/vault/ops/purge_schedule.py` + tray autosync at `vault_submenu.py:223-337`

**Approach:** Wire autosync tick to consume `list_due_purges` + emit `vault.purge.due_awaiting_user` event + system notification. Updated dialog copy to be honest about the online dependency. Full auto-execution requires `purge_secret` persistence (three design options logged for user scoping).

`schedule_purge` writes to a local `purge_state.json`; **nothing reads `list_due_pending_purges`** and calls `gc/execute`. The dialog promises "After {N} hour(s), every chunk and manifest in this vault is deleted from the relay" — user reasonably thinks they can close the laptop and the purge fires. **It cannot.** If the desktop is offline at `scheduled_for_epoch`, nothing happens.

Material UI lie. The dialog asserts a server-side time fuse; the implementation is a local cron that doesn't exist.

**Fix:** wire tray autosync loop to call `gc/execute` for due purges + amend dialog copy. Or push the schedule to the relay so it fires server-side.

#### §6.H2 — No Revoke-device UI — Devices tab is a placeholder — *skipped (new feature build)*
**Status:** logged in `docs/plans/review-doubts.md` §6.H2 — server endpoints ship; desktop needs Devices tab + locked §3.3 wording + fresh-unlock-and-admin double-gate (~500 LOC new GTK + HTTP).
**File:** `desktop/src/windows_vault/main_window.py:188-207`

Devices, Security, Sync safety, Storage tabs are literal placeholder Boxes ("This panel is reserved for later development"). Spec at `vault-architecture.md:1010` mandates "Revoke device grant — Alert confirm with §14-locked wording".

The §3.3 mitigation note's required wording — "Revoking this device prevents future Vault access. It cannot erase data already copied to that device." — is **nowhere in the desktop tree** (`grep -r "Revoking this device" desktop/` empty).

A v1 vault that can grant device access but cannot revoke it has no defence against a lost paired desktop.

**Fix:** build a minimal Devices tab listing active grants with a per-row "Revoke…" button. Use the locked wording verbatim. Gate behind fresh-unlock + admin role.

#### ~~§6.H3~~ — Tray submenu "Sync now" and "Export…" are decorative
**Fix landed:** b3d84ad 2026-05-17
**File:** `desktop/src/tray/vault_submenu.py:138-157, 339-356`

`_vault_sync_now_stub` and `_vault_export_stub` fire a notification saying "this isn't wired yet — open Vault Settings". Memory `feedback_no_fake_tests`: buttons that don't do their advertised work are theatre. An Export bundle is the recommended recovery path for the post-grant scenario (memory `project_vault_multi_device_story`); there's no UI for it at all.

**Approach:** Wired "Sync now" to `_ensure_vault_watcher_runtime()` + `_vault_autosync_kick.set()` — the in-process autosync loop now wakes immediately on click (idempotent, no-op if already running; on exception falls back to the old "check Vault Settings" notification). Removed the "Export…" entry from `vault_submenu_entries`; the wizard build is logged to `review-doubts.md` §6.H3 since it would be a full new GTK subprocess mirroring the import wizard. New events `vault.tray.sync_now.kicked` / `.kick_failed` replace the removed stub events.

#### ~~§6.H4~~ — Passphrase generator window leaks the passphrase
**Fix landed:** 45abecc 2026-05-17
**File:** `desktop/src/windows_vault/passphrase_generator.py:65-87`

**Approach:** `Gtk.PasswordEntry` with peek-icon (obscured by default); 30 s clipboard auto-clear with 1 s countdown via `GLib.timeout_add`; clipboard-manager warning appended to the tip text.

#### ~~§6.H5~~ — Cancel handlers don't honour `on_cancel` contract in Danger flows
**Fix landed:** cd76131 2026-05-17
**File:** `desktop/src/windows_vault/fresh_unlock_prompt.py:199-254`

`tab_danger.py` callsites (lines 166-174, 289-298, 454-476) only pass `on_success`. A cancelled fresh-unlock prompt silently no-ops; user sees the destructive button re-enable with no status feedback — they may think they did clear it.

**Approach:** Each destructive callsite now passes `on_cancel=cancelled` where `cancelled` writes "<op> cancelled." to the danger status label via `_set_danger_status`. Source-pin test asserts ≥3 `on_cancel=` mentions + ≥3 `cancelled.` feedback strings in `tab_danger.py`.

### Medium

#### §6.M1 — Disconnect-vault dialog wording understates impact
**File:** `desktop/src/windows_vault/tab_danger.py:52-79`. The local-data wipe should be visually distinguished, not paragraph text.

#### §6.M2 — Add Folder dialog has no name-collision check
**File:** `desktop/src/vault_folders/dialog_add_folder.py:141-149`. Two "Documents" folders silently coexist; UI dropdowns become ambiguous.

### Low / Info

- **§6.L1** — AT-SPI label includes plaintext filenames (`panes.py:383`). Acceptable; same sensitivity as the visible card title.
- **§6.L2** — Rollback banner copy correctly mentions fresh-device limitation. Spec satisfied.
- **§6.L3** — Activity tab renders destructive events with humanised labels. Spec satisfied.
- **§6.L4** — Cross-window state sync is implicit-via-reload, no inotify. Acceptable for v1.
- **§6.L5** — No subprocess crash detection; tray doesn't offer to re-open. UX rough edge.
- **§6.L6** — `confirm_vault_clear_text_matches` uses `.strip().upper()` — case-insensitive trimmed; matches spec.
- **§6.L7** — Adw 1.4 fallback gate present via `dependency_check.py`. Spec satisfied.
- **§6.L8** — Wizard cancellation correctly preserves toggle (memory `feedback_respect_user_intent` satisfied).
- **§6.L9 (correction)** — H5 from agent report flagged "skip recovery test offered" as missing. Actually that "skip" pattern was deliberately removed (line 437-440 comment). Memory `feedback_no_fake_tests` makes this the correct call. Update the original prompt's invariant to "mandatory recovery test, no skip".

---

## §7. Tests + test vectors

### Critical

#### ~~§7.C1~~ — PHP cross-runtime twin is missing runners for `root_v1.json` and `shard_v1.json`
**Fix landed:** 40e03c2 2026-05-17
**File:** `server/tests/Vault/VaultCryptoVectorsTest.php`

**Approach:** Added `rootCases()` + `shardCases()` data providers mirroring Python's `_run_root_case` / `_run_shard_case`. 12 cases now cross-runtime-pinned (was Python-only pre-fix).

Defines runners for chunk, header, recovery, device_grant, content_fingerprint, export — but NOT root/shard. Python harness exercises 12 root+shard cases including 4 tamper / format-version negatives; PHP only runs 6 of the 8 primitive files. README explicitly says "a vector that breaks one side breaks the build"; Python-only runs can't detect PHP root/shard envelope-builder divergence.

**Fix:** add `rootCases()` + `shardCases()` data providers mirroring `_run_root_case` / `_run_shard_case`. Without this, manifest sharding has **no cross-runtime parity gate**.

#### ~~§7.C2~~ — Controller-level 422 `vault_format_version_unsupported` gate not integration-tested
**Fix landed:** 40e03c2 2026-05-17
**File:** `server/tests/Vault/VaultControllerTest.php`

**Approach:** Four integration tests (putHeader, putRoot, putShard, putShardWithRoot) send a v0x02-tampered envelope at the API surface and assert 422 + `vault_format_version_unsupported`. Covers the wire-ordering aspect that the byte-level vector tests can't.

`VaultController::guardFormatVersion` is called at 4 sites (header, root, shard, root-in-shard-with-root) but the test file has **zero assertions** sending a v0x02 envelope at the API surface and verifying the 422. Byte-level gate is unit-tested in `VaultCryptoVectorsTest`; HTTP wire ordering isn't.

#### ~~§7.C3~~ — No true two-device CAS race + merge end-to-end test
**Fix landed:** fa813a7 2026-05-17
**File:** `tests/protocol/test_desktop_vault_manifest_sharded.py:258` (existing test); new test in `tests/protocol/test_desktop_vault_upload.py`

**Approach:** Add `ConflictInjectingRelay` (FakeUploadRelay subclass) with on_conflict + on_post_fetch hook queues. Queue C and D to advance the head during B's retry cycle. The actual trace shows 4 sequential 409s for B, with all five publishers' versions preserved in the final entry.

`test_simultaneous_upload_same_path_keeps_both_versions` drains A then B sequentially — A wins first publish, B sees one conflict, retries once. The §D4 *merge-retry-merge* cycle (both devices receive 409 with inline current ciphertext, both run auto-merge against same parent, both retry, only one wins second round, loser merges again) is **not exercised**. Add a scenario with 2+ sequential conflicts and assert (a) both terminate, (b) same final root_revision, (c) no entries lost.

### High

#### ~~§7.H1~~ — Cross-vault chunk replay isn't directly tested
**Fix landed:** 421f5be 2026-05-17
The threat: chunk from vault A, decrypted under vault B's master key + AAD-with-vault-B-id, must fail. Generic `test_wrong_aad_fails_closed` covers it transitively; no test explicitly exercises the cross-vault scenario. The AAD-includes-vault_id-failure is the entire security guarantee.

**Approach:** New `CrossVaultChunkReplayTests` in `test_desktop_vault_crypto.py` pins three cases — encrypt under (vault_A, master_A), then assert AEAD raises when decrypting with (master_B, vault_A_AAD) AND with (master_A, vault_B_AAD). Positive control round-trips under (master_A, vault_A_AAD) so the failure mode is anchored.

#### ~~§7.H2~~ — Content fingerprint has zero negative-case coverage
**Fix landed:** 421f5be 2026-05-17
`content_fingerprint_v1.json` has only `happy-path` + `empty-plaintext`. No tamper, no different-master-key-different-output. The HMAC keying is the entire point; without that vector cross-runtime parity is weaker than it appears.

**Approach:** Added two negative vectors with `expected.diverges_from_b64` pinning the inequality — different master_key, then different plaintext_sha256. Both Python `_run_content_fingerprint_case` runner and PHP twin honour the field; case-name frozenset pin updated.

#### ~~§7.H3~~ — Import preview doesn't assert "no writes to relay"
**Fix landed:** 421f5be 2026-05-17
`test_desktop_vault_import.py:86-122` verifies preview shape but doesn't pass a `RecordingRelay` and assert `publish_attempts == 0`, `chunk_uploads == 0`. Memory `feedback_no_fake_tests` applies.

**Approach:** New `test_open_bundle_for_preview_does_not_write_to_relay` captures every FakeUploadRelay write counter (put_calls, chunks, published_shards, published_roots, shard_with_root_puts) before and after the preview run; the only allowed mutation is `batch_head_calls` (the head-count round-trip is part of the preview by design).

#### ~~§7.H4~~ — Export bundle record reorder/index attack not exercised
**Fix landed:** 421f5be 2026-05-17
`test_desktop_vault_export.py:194` covers a single-byte mid-stream tamper; truncation covered. Neither reorders records (e.g. swapping two chunk records' positions). Spec §16: `record_index` is bound into each AAD; reorder should fail.

**Approach:** New `test_chunk_record_reorder_fails_closed` exports a two-chunk bundle, walks the on-disk record-length-prefix structure, swaps the two chunk records' byte ranges, and asserts `read_export_bundle` raises `vault_export_tampered`. Both spec §16's `record_index` AAD bind and the footer hash chain trip on the swap.

#### ~~§7.H5~~ — Migration multi-device discovery via `GET /header.migrated_to` lacks E2E test
**Fix landed:** 421f5be 2026-05-17
PHP side has no test that source relay's `GET /header` returns `migrated_to` post-commit. Desktop side has unit test of the decision; no integration walk-through.

**Approach:** New `test_getHeader_after_commit_carries_migrated_to` in `VaultControllerTest.php` drives the full `/start` → `/verify` → `/commit` sequence then asserts `GET /header` returns the target URL in `migrated_to`. The desktop's `propagate_relay_migration` consumes this signal — pre-fix only the DB row's column was asserted, leaving the public API shape uncovered.

### Medium

- **§7.M1** — Argon2id parameters in vectors are reduced (8 MiB / 2 iter). Production defaults aren't exercised end-to-end.
- **§7.M2** — `test_desktop_vault_folder_runtime.py:209` uses `time.sleep(0.05)`. Migrate to the fake-clock pattern used in `test_desktop_vault_download.py:63`.
- **§7.M3** — Tombstone server-clock authority not asserted. Add a test where request `deleted_at` is back-dated and stored `recoverable_until` is server-now + keep_deleted_days.
- **§7.M4** — Quota 507 with `eviction_available` flag not byte-asserted in `VaultQuotaPressureTest.php`.
- **§7.M5** — AAD tamper coverage per envelope kind incomplete. No per-field AAD-flip chunk vector (e.g., flip chunk_index).

### Low

- **§7.L1** — QR claim race lower bound. No test with two parallel claims with different pubkeys verifying exactly one 200 + one 409.
- **§7.L2** — Case-insensitive local mount collision (`A.txt` vs `a.txt`) untested. Linux primary; cross-mount filesystems (NFS, exFAT) still relevant.
- **§7.L3** — Negative-vector README schema undocumented. Add `tamper`, `envelope_byte_xor`, `aad_override`, `wrapped_key_byte_xor`, `decrypt_passphrase_override` to `tests/protocol/vault-v1/README.md`.

### Info — well-covered (skip these)

- Shard hash chain verification on read: `test_desktop_vault_shard_wire.py:580-626` strong.
- Cross-folder shard AAD replay: `test_desktop_vault_manifest_sharded.py:463`.
- Wrong-passphrase recovery: real Argon2id, two-runtime parity via vectors.
- Frozenset case-name pinning (lines 594-644 of `test_vault_v1_vectors.py`).
- Ransomware threshold tests with deterministic clocks.
- Stability gate timeout cases.
- Tombstone skip during baseline.
- PHP role-enforcement tests (`VaultControllerTest.php:1064-1180`).
- Migration verify/commit/switch-back tests.
- Conflict-name random-hex fallback test.
- Verification-code byte-pin test.
- No `test_mode` / `skip_fresh_unlock` bypasses in production code.

---

## §8. Cross-cutting

### §8.M1 — `docs/vault-critical-risks-evaluation.md` materially overstates shipped state

Multiple resolved-claims are actually unshipped:
- §3.3 mitigation note's required UX wording — "Revoking this device prevents future Vault access. It cannot erase data already copied to that device." — is **nowhere in the desktop tree**.
- §3.3 also says role gates "collapse to admin-only for destructive ops" on manifest/chunk endpoints. Verified: role gates on manifest/chunk endpoints are **complete**. The real gap is **eviction stages 2-3** (§3.C1), which the eval doesn't mention.
- §3.7 rollback detection is "Resolved" — but the per-device floor check fires AFTER cache is updated (§2.H1).
- §3.10 export protection is "Resolved" — but the writer never self-verifies; only the import path does (the eval flags this as deliberate). Fine, but no end-to-end smoke test confirms the property in CI.

**Fix:** re-evaluate §3.3 (role gates: complete on writes, gap on eviction); §3.7 (floor check ordering); add §3.x for the unshipped multi-device UI surface.

### §8.M2 — Architecture doc + spec drift from code

Several places where `docs/vault-architecture.md` is forward-looking past what ships:
- §11 says "Migration is initiated by one device. Others learn on their next `GET /header`". Receiver-side propagation isn't wired (§5.C3).
- §13 lists QR-assisted device join + revoke + access-secret rotation in the v1 UI surface. None are wired (§5.C1, §5.C2, §5.H3, §6.H2).
- §13 says default unlock timeout is 15 min; code uses 120 s (§2.H3).
- The "pre-sharding single envelope never shipped" claim is mostly true but `browser_model.py` still falls back to it (§2.M4).

**Fix:** update the architecture doc to mark these as v1.x, OR ship them in v1.

### §8.M3 — Spec § 10 rate limits never landed (§1.H1)

The wire spec mandates them; the relay doesn't enforce them. Decide: drop from spec, or implement.

### §8.M4 — Test isolation memory note: verify constructor-path isolation

Memory `feedback_test_isolation` records the 2026-05-06 incident where dev twin overwrote real auth token. Verify the vault test harness:
- Separate config dir? Yes — `Config` derives keyring service from `config_dir.name`.
- Separate relay? Yes — `tests/vault-tests.md` covers it.
- Constructor-path isolation, not env-var-only? Verified in `vault/grant/store.py:53-75`.

✅ Holds.

### §8.M5 — Debug-bundle leak scan

`vault/diagnostics/debug_bundle.py` redaction is well-designed per the architecture doc. The narrow URL-safe-base64 + token-field-name gap was closed in `plans/live-testing-followup.md` §9. No new findings.

---

## §9. Recommended ship gate

Before stamping v1, in priority order:

1. **Fix the 9 Criticals.** Each is a 50–500-line patch.
2. **Land at least these Highs:** §1.H1 (rate limit), §1.H2 (state-gated getChunk), §3.H2 (download cancellation), §3.H3 (RAM stream), §3.H4 (trash fallback), §3.H5 (watcher lock), §3.H1 (detector ordering), §6.H1 (purge executor), §6.H2 (revoke UI), §2.H1 (floor ordering), §5.H1 (genesis fingerprint), §6.H4 (passphrase clipboard). The rest can go to v1.0.1.
3. **One regression test per Critical:** chunk re-PUT after GC, gc orphan reaper, eviction admin gate, symlink rejection in scan/preflight, migration token re-issue, watcher cancellation on detector trip, browser folder-delete typed-confirm, hard-purge admin gate, multi-device migration propagation, QR-join roundtrip.
4. **Add PHP root/shard vector runners (§7.C1) and a 422-format-version integration test (§7.C2) before next CI run.**
5. **Update the docs/`docs/vault-critical-risks-evaluation.md`** with the actual state. Don't ship a "v1" eval doc that describes an aspirational v1.1.

Doable in a focused week of work plus a second week of testing. **Do not ship until done.**

---

## Appendix A — Verification log

Findings I read-verified end-to-end (not just trusted the investigator):

- **§1.C1** — Read `VaultController.php:825-891` and `VaultChunksRepository.php:104-130, 177-180`. Confirmed `head()` doesn't filter by state; `put()` short-circuits.
- **§1.C2** — Read `VaultController.php:1199-1235`. Confirmed unlink loop outside tx, no second-pass reaper, next `gcExecute` skips purged rows.
- **§1.C3** — Read `VaultController.php:1326-1357`. Confirmed token generation precedes existence check.
- **§3.C3** — Read `scan.py:55-99` and `preflight.py:211-217`. Confirmed `path.stat()` follows symlinks. Compared to `baseline.py:263-285` which uses `os.lstat()` correctly.
- **§2.M4** — Read `browser_model.py:85-108`. Confirmed legacy fallback path still alive.
- **§5.C1, §5.C2, §5.C3** — Ran `grep -RE "run_migration\b|make_join_url\b|propagate_relay_migration\b" desktop/src/ tests/` — verified zero non-test callers for migration runner, QR helpers, and propagation function.
- **Migration 004 contains `vault_access_secret_rotations` table** — false alarm from an investigator agent; verified the table exists in `server/migrations/004_vault_device_grants.sql:38`. Removed from finding list.

Findings I sample-verified (read cited lines, trusted reproduction details):

- §1.H1, §1.H2, §1.H3, §1.H4, §1.H5, §1.H6, §1.M1–§1.M7
- §2.H1, §2.H2, §2.H3, §2.M1, §2.M2, §2.M3, §2.M5
- §3.C1, §3.C2, §3.H1–§3.H9, §3.M1–§3.M6
- §4.H1–§4.H5, §4.M1–§4.M4
- §5.H1, §5.H2, §5.H3, §5.H4, §5.M1–§5.M6
- §6.C1, §6.C2, §6.C3, §6.C4, §6.C5, §6.H1–§6.H5, §6.M1, §6.M2
- §7.C1, §7.C2, §7.C3, §7.H1–§7.H5, §7.M1–§7.M5

Anyone implementing a fix should re-read the cited file:line before committing — agents make typos in line numbers.

## Appendix B — Findings that did not survive verification

For honesty's sake: things investigator agents flagged that turned out to be wrong on second look.

1. **`vault_access_secret_rotations` table missing from migrations** — false. It's in `004_vault_device_grants.sql:38`. Searched `grep -RE "vault_access_secret_rotations" server/`.
2. **Skip-recovery-test option missing from wizard** — by design. The skip pattern was deliberately removed for memory-policy compliance (`feedback_no_fake_tests`); `onboard_window.py:437-440` documents the removal.
3. **AAD byte-count invariant tests "missing"** — partially true. Python `crypto.py` has length assertions in every builder; PHP only has length assertions in root + shard. The byte-pinned test vectors at `tests/protocol/vault-v1/*.json` exercise the byte-exact AAD via the vector runner. A generic invariant guard (e.g. `assertSame(76, strlen(buildRootAad(...)))`) would catch the specific regression class "schema string accidentally lowercased" but the existing vector pin already does this transitively. Flagged as Medium not Critical.

---

**End of review.** Total findings: 17 Critical, 37 High, 22 Medium, ~30 Low/Info. The team's evaluation labelling all 20 critical risks Resolved/Mitigated does not survive independent inspection.

Critical clusters that need addressing before v1:
- **Server chunk-state lifecycle** (§1.C1, §1.C2, §1.H2) — chunk lifecycle and GC have a class of correctness bugs around the `purged`/`gc_pending` states that the server logic doesn't consistently honour.
- **Migration state machine** (§1.C3, §5.C1, §5.C3) — runner library exists; the UI driver, the multi-device propagation, and the start-token re-issue path do not.
- **Multi-device UI surface** (§5.C2, §6.H2, §5.H3) — QR-join, revoke, and access-secret rotation are library-only. The architecture doc claims them shipped.
- **Destructive-action guards** (§3.C1, §6.C4, §6.C5, §6.H1) — eviction stages 2-3 hard-purge without admin gate; browser hamburger bypasses Danger-zone guards; hard-purge has no admin gate AND no executor.
- **Sync engine race conditions** (§3.C2, §3.C3, §3.H1, §3.H5) — ransomware detector + symlink follow + threading + ordering bugs that collectively make sync engine unreliable on attack scenarios.
- **UI thread crypto** (§6.C1, §6.C2, §6.C3) — Argon2id on GTK main thread causes perceived crash on mandatory paths.

These clusters compose. Many fixes are independent and short; some (multi-device UI surface) are real new work. A focused week of fixes + a week of regression tests is realistic.
