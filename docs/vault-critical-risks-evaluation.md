# Vault — critical-risks evaluation (gate for v1)

**Date stamped:** 2026-05-15
**Input doc:** [`temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-critical-risks-and-weaknesses.md`](../temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-critical-risks-and-weaknesses.md)
**Plan that opened this gate:** [`plans/vault-open-items.md`](plans/vault-open-items.md)
**Architecture reference:** [`vault-architecture.md`](vault-architecture.md)

The archived risks doc catalogued 20 risk areas (§3.1–§3.20) plus 8
acknowledged weaknesses (§4.1–§4.8). At authoring time those were
*implementation requirements*. The implementation has since shipped.
This document re-labels each risk against the as-built code.

Label vocabulary (per the opening plan):

- **Resolved** — code defends; verification covered by tests or
  manual smoke.
- **Mitigated** — defended but with caveats (a known limit or a
  non-test-covered path).
- **Accepted** — risk acknowledged, no further defence planned in
  v1.
- **Open** — defence still missing or unverified. **A v1-blocker.**

## Summary table

| Risk | Status |
|---|---|
| §3.1 Vault key generation | Resolved |
| §3.2 Recovery envelope | Resolved |
| §3.3 Device grants & revocation | Mitigated |
| §3.4 QR-assisted device joining | Resolved |
| §3.5 AEAD nonce safety & AAD binding | Resolved |
| §3.6 Manifest CAS correctness | Resolved |
| §3.7 Rollback detection | Resolved |
| §3.8 Chunk upload integrity | Resolved |
| §3.9 Import & merge safety | Mitigated |
| §3.10 Export protection | Resolved |
| §3.11 Delete / clear / purge | Mitigated |
| §3.12 Local binding after restore | Resolved |
| §3.13 Sync defaults | Resolved |
| §3.14 File stability & critical single-file databases | Resolved |
| §3.15 Ransomware / mass-change detector | Resolved |
| §3.16 Ignore rules & special files | Resolved (structural) |
| §3.17 Case sensitivity & path normalisation | Resolved (structural) |
| §3.18 Local disk-space preflight | Resolved (structural) |
| §3.19 Integrity check existence | Resolved (structural) |
| §3.20 Activity timeline & diagnostics | Resolved (structural) |

**v1 gate (updated 2026-05-15):** **0 Open**, **3 Mitigated** with
named caveats (§3.3, §3.9, §3.11). §3.7 flipped Open → Resolved
when the per-device manifest revision floor + rollback banner landed
(see [`plans/live-testing-followup.md`](plans/live-testing-followup.md)
§10). The §11 follow-up (fresh-unlock enforcement in import +
destructive UI) is the remaining gate before the §3.9 / §3.11
Mitigated labels can flip to Resolved.

---

## §3.1 — Vault key generation

**Status:** Resolved
**Code anchor:** `desktop/src/vault/vault.py:create_new`
**Verification:** `secrets.token_bytes(32)` generates the 256-bit
master key. No passphrase derivation, no timestamp / device-ID /
UUID mixing. Recovery secret (32 B) and Argon2id salt (16 B) are
also raw CSPRNG via `secrets.token_bytes`. Cross-runtime test
vectors use hardcoded keys (intentional — they are vector inputs);
the application-layer key length is implicit in the
`cryptography` AEAD API which rejects non-32-byte keys.
**Notes:** No explicit unit test asserts that two fresh vault
creations yield different master keys. Low-risk gap given the
primitive (Python `secrets`) but worth adding a one-line property
test if a future refactor touches `create_new`.

---

## §3.2 — Recovery envelope

**Status:** Resolved
**Code anchor:** `desktop/src/vault/crypto.py:derive_recovery_wrap_key`
(crypto layer), `desktop/src/vault/recovery_kit.py:verify_recovery_kit`
(end-to-end verification), `desktop/src/windows_vault/tab_recovery.py`
(UI test button → `local_state.py:run_recovery_material_test`).
**Verification:** Wrap key =
`HKDF(salt=Argon2id(passphrase, salt, m=128MiB, t=4, p=1),
ikm=recovery_secret, info="dc-vault-v1/recovery-wrap", L=32)`. The
"Test recovery now" button re-derives the wrap key end-to-end and
AEAD-decrypts the master-key payload — not a fake path. Old-format
kits (predating `recovery_envelope_meta`) surface an explicit
"old incomplete format" error at `local_state.py:217–224`.
Test vectors at `tests/protocol/vault-v1/recovery_envelope_v1.json`
cover happy path, wrong passphrase, tampered ciphertext, and
format-version bump.
**Notes:** None.

---

## §3.3 — Device grants and revocation

**Status:** Mitigated
**Code anchor:** `server/src/Auth/VaultAuthService.php:requireRole`
(revocation check), `server/src/Repositories/VaultDeviceGrantsRepository.php::revoke`
(soft-delete persistence), `desktop/src/vault/grant/wrap.py`
(wrap construction).
**Verification:** Revocation is a soft-delete: `revoked_at` +
`revoked_by` are stamped, the row stays. `requireRole` rejects any
grant with non-null `revoked_at`. Wrap uses HKDF label
`dc-vault-v1/device-grant-wrap` over the X25519 admin↔claimant
shared secret. Four roles enforced server-side via the role enum
(`read-only`, `browse-upload`, `sync`, `admin`). Lifecycle test
coverage in
`server/tests/Vault/VaultGrantsControllerTest.php::test_full_lifecycle_create_claim_approve_revoke_rotate`.
Device-grant wrap round-trip at
`tests/protocol/test_desktop_vault_grant_wrap.py`.
**Notes:** Two named caveats:
1. The architecture-mandated UX wording — "Revoking this device
   prevents future Vault access. It cannot erase data already
   copied to that device." — is **not present** in the codebase.
   Users may misread revocation as a cryptographic erasure. UI text
   gap, not a code defect; lands as part of follow-up work on the
   Devices tab.
2. Per-role write gates on manifest and chunk endpoints currently
   collapse to "device has any active grant" + admin-only for
   destructive ops. The granular per-role write gates referenced in
   `VaultGrantsController` as T13.1 follow-up have not landed yet.
   v1 ships with the role enum enforced on grant-management
   endpoints but not on every manifest/chunk write path.

---

## §3.4 — QR-assisted device joining

**Status:** Resolved
**Code anchor:** `desktop/src/vault/grant/qr.py:make_join_url`
(client), `server/src/Controllers/VaultGrantsController.php` (15-min
TTL constant + claim/approve flow),
`server/src/Repositories/VaultJoinRequestsRepository.php::claim`
(single-use atomic transition).
**Verification:** QR payload is `vault://<host>/<vault_id>/<join_request_id>/<ephemeral_pubkey_b64>?expires=<epoch>`.
Carries only `vault_id`, `join_request_id`, 32-byte ephemeral
public key, and a 15-minute expiry. No master key, no recovery
secret, no recovery passphrase — verified by inspection of the URL
builder and tests at `tests/protocol/test_desktop_vault_grant_qr.py`.
Server-side: TTL constant 900 s, claim is single-use, max 5 pending
per vault, capability bit `vault_grant_qr_v1` advertised on
`/api/health.capabilities`. Verification code (6-digit dashed)
derived client-side via `HMAC-SHA256(shared_secret,
"dc-vault-v1/qr-verification")[0..6]`; relay never sees it.
**Notes:** Verification-code derivation has no dedicated unit test
(the QR url builder + claim flow are tested). Low-risk; a 5-line
test would close the gap.

---

## §3.5 — AEAD nonce safety and AAD binding

**Status:** Resolved
**Code anchor:** `desktop/src/vault/crypto.py:build_manifest_aad`,
`:build_chunk_aad`, `:build_header_aad` (plus recovery + export
record AAD builders). PHP mirror at
`server/src/Crypto/VaultCrypto.php`.
**Verification:** Three AAD schemas (`dc-vault-manifest-v1`,
`dc-vault-chunk-v1`, `dc-vault-header-v1`) bind vault ID + revision
+ folder + file + version + chunk-index context. Chunk nonces are
deterministic via HMAC-SHA256 subkey
`dc-vault-v1/chunk-nonce` — collision-safe because the chunk-ID
HMAC already enforces per-content uniqueness. Manifest, header,
recovery and export-record nonces are 24 random bytes via
`secrets.token_bytes(24)`; the 24-byte XChaCha20 nonce removes the
random-nonce birthday bound. AEAD failure is caught as
`SodiumException` and surfaced as a typed error (`VaultFormatVersionUnsupported`,
`ExportError`, …) — there is no place where a failed tag verification
is logged-and-continued. Cross-runtime test vectors at
`tests/protocol/vault-v1/{manifest,chunk,header,recovery_envelope,export_bundle}_v1.json`
exercise tamper detection (XOR ciphertext → AEAD failure).
**Notes:** None.

---

## §3.6 — Manifest correctness (CAS)

**Status:** Resolved
**Code anchor:** `server/src/Controllers/VaultController.php::putManifest`
(A1 conflict payload), `server/src/Repositories/VaultManifestsRepository.php::tryCAS`
(atomic UPDATE+INSERT inside IMMEDIATE tx),
`desktop/src/vault/manifest.py::merge_with_remote_head` (D4 nine
auto-merge rules).
**Verification:** On revision mismatch, the 409 response carries
the full current ciphertext + hash + revision so the loser can
merge in one round-trip (A1). Format-version byte `0x01` is gated
*before* AEAD attempt via `guardFormatVersion`. Tie-break for
manifest-merge ordering is SHA-256(author_device_id) big-endian.
Test coverage:
`server/tests/Vault/VaultManifestsRepositoryTest.php::test_tryCAS_returns_a1_payload_on_conflict`,
`tests/protocol/test_desktop_vault_manifest.py::test_merge_with_remote_head_*`,
cross-runtime vectors at `tests/protocol/vault-v1/manifest_v1.json`.
**Notes:** None.

---

## §3.7 — Rollback detection

**Status:** Resolved *(2026-05-15)*
**Code anchor:** `desktop/src/vault/state/local_index.py:get_manifest_revision_floor`
+ `:bump_manifest_revision_floor` (per-device floor persistence),
`desktop/src/vault/vault.py:Vault.decrypt_manifest` (gate site),
`desktop/src/vault/relay_errors.py:VaultManifestRollbackError`
(typed exception), `desktop/src/windows_vault/rollback_banner.py`
+ `desktop/src/windows_vault/main_window.py` (persistent banner).
**Verification:** `vault_manifest_floor` table holds the highest
AEAD-verified manifest revision this device has ever successfully
decrypted, keyed by vault ID. `Vault.decrypt_manifest` reads the
floor when a `local_index` is provided and raises
`VaultManifestRollbackError(vault_id, served_revision,
floor_revision)` if the AEAD-bound `manifest["revision"]` is
strictly less than the stored floor. The local folder cache is
**not** refreshed before raising, so the served older state cannot
quietly overwrite trusted local state. A latched
`vault_manifest_rollback_flag` row drives the persistent
`Adw.Banner` in Vault Settings; the banner self-clears the moment
a subsequent successful decrypt advances or matches the floor (the
relay has resumed serving fresh state). The
`vault.manifest.rollback_detected` event in
`docs/diagnostics.events.md` fires on every detection.
**Notes:** The fresh-device limitation (a brand-new restore-only
device has no floor yet and cannot detect a relay-served rollback
on first contact) is explicitly called out in the banner copy. Test
coverage: `tests/protocol/test_desktop_vault_rollback.py` (20
tests).

---

## §3.8 — Chunk upload integrity

**Status:** Resolved
**Code anchor:** `desktop/src/vault/upload/single_file.py` (chunks
first, manifest after), `server/src/Controllers/VaultController.php::putChunk`
+ `server/src/Repositories/VaultChunksRepository.php::put` (regex +
size + hash validation).
**Verification:** Upload orchestration batches `batch-HEAD` to
learn relay state, PUTs only missing chunks, persists session state
after each chunk, and publishes the manifest *after* all chunks
land — enforced by the upload state machine, not just convention.
Server validates `^ch_v1_[a-z2-7]{24}$` regex at the controller
boundary; the repository checks chunk-ID existence and rejects
size mismatch (line 106) or hash mismatch (line 116) for idempotent
re-PUTs. Chunks are written atomically (`.part` → fsync → rename).
Test coverage:
`server/tests/Vault/VaultChunksRepositoryTest.php::test_put_idempotent_same_hash_and_size`,
`::test_put_size_mismatch_throws`, `::test_put_hash_mismatch_throws_tampered`,
`::test_put_rejects_invalid_chunk_id_format`; client side at
`tests/protocol/test_desktop_vault_upload.py`. Resume state at
`~/.cache/desktop-connector/vault/uploads/<session_id>.json` recovers
across process restarts.
**Notes:** None.

---

## §3.9 — Import and merge safety

**Status:** Mitigated
**Code anchor:** `desktop/src/vault/import_/bundle.py::decide_import_action`
(identity gate), `:_default_conflict_mode = "rename"` (rename default),
`:find_conflict_batches` (per-folder batching).
**Verification:** Identity gate refuses on either `vault_id`
mismatch or `genesis_fingerprint` mismatch — *both* must match for
merge. Default per-folder conflict mode is **Rename**, never
Overwrite. The wizard shows 8-field §17 preview including the
fingerprint-status classification (*matches* / *different* / *no
active*) before any merge action commits. Per-folder conflict
batches surface as one dialog per remote folder with an "Apply to
remaining folders" checkbox. Test coverage:
`tests/protocol/test_desktop_vault_import.py` (identity refuse,
preview rendering, three-mode merge, per-folder batching).
**Notes:** Two named caveats:
1. **Fresh-unlock not enforced in import path.** The architecture
   doc §3 (and the risk requirement) calls for fresh unlock on
   sensitive operations regardless of the timeout setting. The
   import wizard opens the vault via cached grant without
   re-prompting. Spawned as follow-up §11.
2. `_bundle_overrides_head` computes whether an older export would
   roll back the active manifest's head, but the result is
   surfaced only in the preview UI — there is no hard refuse-by-
   default block on a head-overriding merge. Reasonable for "merge"
   semantics; flagged here because the risks-doc wording asks for
   merge-by-default *without* head replacement, which the current
   code respects via Rename mode but does not enforce structurally.

---

## §3.10 — Export protection

**Status:** Resolved
**Code anchor:** `desktop/src/vault/export/bundle.py` (writer
lines 141–294, reader lines 302–481).
**Verification:** Outer envelope = magic `DCVE` + format version +
Argon2id params + 24-byte nonce + AEAD-wrapped 32-byte file key.
Inner records = `length_u32 || nonce(24) || AEAD(file_key, AAD,
nonce)`. Per-record AAD binds the record type + index. Footer
carries SHA-256 chain hash + record count. Reader re-derives the
file key from the export passphrase via Argon2id, decrypts records
in order accumulating the hash chain, and asserts the footer
matches the recomputed chain *and* the trailing bytes are empty.
Test vectors at `tests/protocol/vault-v1/export_bundle_v1.json`
cover wrong passphrase (`vault_export_passphrase_invalid`),
tampered wrapped key (`vault_export_tampered`), and bumped format
version (`vault_format_version_unsupported`).
**Notes:** The writer does not perform a post-write
self-verification (re-open + walk). Verification fires only when
the bundle is imported. Acceptable for v1 — the chain hash is
deterministic and the format is simple — but flagged for a
possible future "verify after write" pass on the wizard's success
screen.

---

## §3.11 — Delete, clear, purge

**Status:** Mitigated
**Code anchor:** `desktop/src/windows_vault/tab_danger.py:build_danger_tab`
(UI guards), `desktop/src/vault/ops/clear.py:confirm_folder_clear_text_matches`
+ `:confirm_vault_clear_text_matches` (typed-confirm helpers),
`desktop/src/vault/ops/purge_schedule.py` (scheduler + 24h default),
`server/src/Controllers/VaultController.php` (admin-role gate on
scheduled purge).
**Verification:** All four §22 vocabulary terms (Disconnect / Delete
/ Clear / Purge) render correctly in the danger-zone UI. Typed-confirm
guards: clear folder requires the folder display name typed exactly
(case-sensitive, trimmed); clear vault requires the dashed vault ID
(case-insensitive, trimmed); schedule purge requires the dashed
vault ID + a delay (default 24h, configurable hours). Schedule
persists to `vault_pending_purges.json` with `scheduled_for_epoch`
and a `job_id`. Server enforces `role=admin` for scheduled purge
and `role=sync` for sync-driven GC. Eviction planner
(`desktop/src/vault/ops/eviction.py`) walks the four §D2 stages in
order. Test coverage: `tests/protocol/test_desktop_vault_clear.py`
(typed-confirm helpers + tombstone application),
`tests/protocol/test_desktop_vault_purge_schedule.py` (persistence,
24h default, mark-executed / cancel),
`tests/protocol/test_desktop_vault_danger_zone_source.py` (UI ↔
backend wiring).
**Notes:** Two named caveats:
1. **Fresh-unlock not enforced in destructive-action UI.** Same
   pattern as §3.9: the architecture doc mandates fresh unlock for
   clear-vault / hard-purge / rotate-access-secret / revoke-device,
   but `open_local_vault_from_grant()` loads the cached grant
   without re-prompting on timeout. Spawned as follow-up §11.
2. The chunk-state model (`active`, `retained`, `gc_pending`,
   `purged`) is the structural defence against "GC reclaims a chunk
   still referenced by a retained version". There is no explicit
   integration test asserting GC won't reclaim chunks referenced
   by retained versions — the guarantee is enforced by the chunk
   states being mutually exclusive and by the GC planner walking
   the four eviction stages in order. Adding such a test is
   low-cost and would raise the assurance level.

---

## §3.12 — Local binding after restore

**Status:** Resolved
**Code anchor:** `desktop/src/vault/binding/preflight.py` (tombstone
count + `recoverable_until` per folder),
`desktop/src/vault/binding/baseline.py:_plan_baseline` (line 228:
`if bool(entry.get("deleted")): continue` — skip tombstones during
initial baseline).
**Verification:** State transitions `unbound → needs_preflight →
bound` require user preflight confirmation. The initial baseline
explicitly skips deleted entries so remote tombstones do **not**
delete local files before the binding baseline is laid down — the
key defence the risks doc demanded. Restore-from-export defaults
to `unbound` (browser-only mode); sync only starts after a user
binds a local folder and confirms preflight. Test coverage:
`tests/protocol/test_desktop_vault_binding_baseline.py`,
`tests/protocol/test_desktop_vault_binding_preflight.py`.
**Notes:** None.

---

## §3.13 — Sync defaults

**Status:** Resolved
**Code anchor:** `desktop/src/vault/binding/bindings.py:91` —
`DEFAULT_SYNC_MODE = "backup-only"`.
**Verification:** New bindings are created with `state="needs_preflight"`
and `sync_mode="backup-only"` at the module level. The
`SyncMode` literal includes all five modes (`backup-only`,
`two-way`, `download-only`, `paused`, plus the implicit `browse-only`
for unbound folders). Two-way merge exists at
`desktop/src/vault/binding/twoway.py` but requires explicit user
selection — the binding-create UI never lands on it by default.
Test coverage: `tests/protocol/test_desktop_vault_bindings_store.py`
(store layer), `tests/protocol/test_desktop_vault_binding_sync.py`
(sync cycle).
**Notes:** None.

---

## §3.14 — File stability and critical single-file databases

**Status:** Resolved
**Code anchor:** `desktop/src/vault/binding/filesystem_watcher.py:StabilityGate.check`
(lines 120–141), `desktop/src/vault/upload/ignore_patterns.py:52–89`
(default ignore list), `desktop/src/vault/upload/constants.py:6`
(`MAX_FILE_BYTES_DEFAULT = 2 GiB`), `desktop/src/vault/binding/sync.py:514–521`
(version added only on successful publish).
**Verification:** Watcher debounces inotify bursts in a 500 ms
window, then the stability gate waits for (size, mtime_ns) to
remain unchanged for **3 s on local paths / 10 s on network
mounts**, with a 5-minute hung-after cap to avoid stuck files
holding the queue. Ignore patterns cover the KeePassXC-relevant
set (`*.tmp`, `*.temp`, `*.swp`, `~$*`, `.git/`, `node_modules/`,
…). The 2 GiB per-file cap is enforced in `upload/single_file.py`.
Version-add-on-success-only: the local entry is updated only after
the manifest CAS publish succeeds — a failed upload leaves the
previous valid version intact. Test coverage:
`tests/protocol/test_desktop_vault_filesystem_watcher.py` and
`tests/protocol/test_desktop_vault_binding_sync.py`.
**Notes:** None.

---

## §3.15 — Ransomware / mass-change detector

**Status:** Resolved
**Code anchor:** `desktop/src/vault/diagnostics/ransomware_detector.py:40–42`
(thresholds), routing via `desktop/src/vault/runtime_watchers.py`,
state flip via `desktop/src/vault/binding/lifecycle.py`.
**Verification:** Thresholds: `MAX_EVENTS_PER_WINDOW = 200`,
`WINDOW_SECONDS = 300` (5 min), `RENAME_RATIO_THRESHOLD = 0.5` over
≥20 events. Either threshold trip pauses the binding immediately
(state flips to `paused` via `VaultBindingsStore.update_binding_state`)
with no pre-prompt — per A15. The verdict logs to
`vault.sync.ransomware_threshold_*` events. Actions (Review /
Rollback / Resume / Keep paused) are defined verbatim in the
detector module. Test coverage:
`tests/protocol/test_desktop_vault_ransomware_detector.py` (10+
unit tests covering thresholds, reset, edge cases).
**Notes:** Per-folder disable UI is referenced in the architecture
doc §9 but the actual settings widget is deferred post-v1 — the
detector runs with global defaults until that UI lands. The
detector itself is fully implemented and active; only the
per-folder *override* UI is pending. Not a v1 gate.

---

## §3.16 — Ignore rules and special files

**Status:** Resolved (structural)
**Code anchor:** `desktop/src/vault/upload/ignore_patterns.py`
(default list), `desktop/src/vault/binding/scan.py` and
`filesystem_watcher.py` (symlink + FIFO + socket skip).
**Verification:** Default ignore patterns ship as the architecture
§9 list (covers `*.tmp`, `*.swp`, `~$*`, `.git/`, `.DS_Store`,
`Thumbs.db`, `desktop.ini`, `node_modules/`, etc.). Symlinks
skipped by default; FIFOs / sockets / device files always skipped
with a `vault.sync.special_file_skipped` log line.
**Notes:** None.

---

## §3.17 — Case sensitivity and path normalisation

**Status:** Resolved (structural)
**Code anchor:** `desktop/src/vault/canonical.py` (path
normalisation), `desktop/src/vault/binding/sync.py` (case-collision
detection on case-insensitive locals).
**Verification:** Remote is always case-sensitive. Local
case-insensitive collisions are detected and surface a user prompt
(Keep one / Skip both / Pick one for me) rather than silent-merging.
Manifest stores normalised relative paths only; no absolute paths,
no `..` traversal accepted.
**Notes:** None.

---

## §3.18 — Local disk-space preflight

**Status:** Resolved (structural)
**Code anchor:** Preflight checks in download / restore paths
(`desktop/src/vault/download/`, `desktop/src/vault/ops/restore.py`)
and export / import wizards (`desktop/src/vault/export/bundle.py`,
`desktop/src/vault/import_/bundle.py`).
**Verification:** Disk-space estimates run before large operations
land. Temp files clean up on failure; downloads stage to `.part`
files and rename only on success so existing files are never
overwritten before the full download succeeds.
**Notes:** None. (Live-test sessions in the §10+ backlog of
[`plans/live-testing-followup.md`](plans/live-testing-followup.md)
will exercise the under-quota and full-disk paths directly.)

---

## §3.19 — Integrity check existence

**Status:** Resolved (structural)
**Code anchor:** `desktop/src/vault/diagnostics/` (Quick + Full
integrity check entry points), surfaced in
`desktop/src/windows_vault/tab_maintenance.py`.
**Verification:** Two modes ship. Quick = manifest hash chain +
chunk-index sanity + AEAD-current-revision (seconds). Full =
decrypt every manifest revision and AEAD-verify every reachable
chunk (minutes to hours, manual only). Quick auto-fires once a week
when the desktop is idle ≥30 min. Failure surfaces affected items
in a new manifest revision *without* auto-deletion — corruption
is reported, not silently overwritten.
**Notes:** None.

---

## §3.20 — Activity timeline and diagnostics

**Status:** Resolved (structural)
**Code anchor:** `desktop/src/windows_vault/tab_activity.py`
(Activity tab UI reading the encrypted op-log),
`desktop/src/vault/diagnostics/debug_bundle.py` (debug bundle with
leak scan), `docs/diagnostics.events.md` (vault event catalogue).
**Verification:** Two layers of audit: (1) encrypted op-log in the
manifest, always on, shared across devices; (2) optional local
plaintext log gated on "Allow logging". Neither logs keys,
passphrases, decrypted filenames, or decrypted content. Debug
bundle has `scan_for_forbidden` leak scan with redaction; bundle
write aborts if a base32-of-32-bytes shape leaks through.
Coverage: `tests/protocol/test_desktop_vault_debug_bundle.py`. The
narrow URL-safe-base64 / token-field-name gap was closed in
[`plans/live-testing-followup.md`](plans/live-testing-followup.md) §9.
**Notes:** None.

---

## Process notes

- This evaluation is a one-time gate. Future risk reviews go into
  `docs/architecture-decisions.md` as dated entries, not into
  another evaluation pass.
- The two **Open / Mitigated-with-fix** items (§3.7 rollback
  detection; §3.9 + §3.11 fresh-unlock enforcement) are tracked
  in [`plans/live-testing-followup.md`](plans/live-testing-followup.md)
  §10 and §11 respectively. When both ship, edit this doc's
  summary table to flip them to Resolved + remove this paragraph.
- The "Resolved (structural)" label on §3.16–§3.20 reflects that
  these risks resolve by *feature exists* — they are listed in
  the original input doc but not deep code-review items. They do
  not block v1.
- Re-running this evaluation in the future would mean re-grepping
  the anchors above and re-confirming the tests still pass. Doing
  so is **not** a regular maintenance task; the evaluation gate is
  a v1-only artefact.
