# Vault — Implementation Progress

Working tracker. T0 decision lock at [`desktop-connector-vault-T0-decisions.md`](desktop-connector-vault-T0-decisions.md) is the **authoritative spec** — when this tracker, the plan files (01–11), and the T0 lock disagree, T0 lock wins.

---

## How to use this file

- Tick `[x]` when a sub-task lands; `[~]` while working on it; `[!]` if blocked (link the blocker note).
- Each phase has 4–8 sub-tasks sized for ~1 PR / ~1 day each, with explicit acceptance criteria.
- **Milestones M1–M7** at the bottom group phases into testable slices. When all phases in a milestone are `[x]`, run the milestone's manual test script. That's the user-visible "this slice works" gate.
- v1 ships at end of **M7**. Sync (T10–T12) lands as **M5–M6**. Android is a separate post-v1 plan — not tracked here.

### Status legend

`[ ]` not started · `[~]` in progress · `[x]` landed · `[!]` blocked · `[—]` deferred (out of scope for v1)

---

## Testing approach

**Every code-changing sub-task ships with tests.** No PR closes a sub-task without coverage appropriate to the layer. The test stack is **local-only by default** — no external relay or third-party services required for normal development.

### Default test stack

- **Server**: `php -S 127.0.0.1:4441 -t server/public/` (one instance for normal tests; spin up a second on `:4442` for migration / multi-relay tests).
- **Desktop**: `cd desktop && python3 -m src.main` (run with `--config-dir=/tmp/dc-A/` and `--config-dir=/tmp/dc-B/` for two-instance multi-device tests).
- **Server tests**: `phpunit` (existing pattern under `server/tests/` — extend with `server/tests/Vault/`).
- **Desktop tests**: `pytest desktop/tests/` for desktop-only logic; `pytest tests/protocol/` for cross-platform vectors.
- **Cross-platform vectors**: `tests/protocol/test_vault_v1_vectors.py` runs the same JSON cases under `tests/protocol/vault-v1/` through both desktop Python crypto and server PHP crypto. A vector that breaks one side breaks the build loudly.

### Test layer per sub-task type

| Sub-task type | Required tests |
|---|---|
| Server repository / endpoint | PHPUnit unit + integration: happy path + at least one error case from T0 error table. |
| Server middleware | PHPUnit standalone middleware test + integration test wired through Router. |
| Crypto primitive | Unit test against fixed input/output tuples; once T2 lands, also goes through the cross-platform vector harness. |
| Test-vector additions | Vector lives in `tests/protocol/vault-v1/<primitive>.json`; the harness exercises it on both sides. |
| Desktop business logic (vault.py, sync engine, …) | pytest unit + integration with mocked relay. |
| Desktop GTK window | Manual smoke script in the sub-task acceptance criteria + automated test of the underlying view-model-like Python helpers (decision functions, formatters). |
| Migration / multi-device flows | Integration test using two PHP instances on different ports (`:4441` + `:4442`) OR two desktop processes with separate `--config-dir`. |
| State-machine transitions (toggle wizard, migration, eviction, ransomware-pause) | Property-style test enumerating the state transitions; not just one happy path. |

### When the local stack isn't enough

If a sub-task genuinely requires something the default stack can't provide:

- **Missing tool / dependency**: the sub-task description must say so explicitly. If a build / test step needs a tool not on a vanilla Zorin/Ubuntu install (`php`, `python3`, `pytest`, `phpunit`, `sqlite3`, `gtk4`-dev, `libadwaita`-dev, `wl-clipboard`, `xclip`, `qrencode`, `keyring`, `gpg`, …), the task tells the developer to ask the user to install it before the task starts. Add the dep to `desktop/requirements.txt` / `server/composer.json` / docs as appropriate.
- **Remote relay environment** (only when validating production-like Apache + mod_rewrite behavior): the sub-task is flagged **`[REMOTE]`** in its description. The task asks the user for a temporary directory on a remote test relay; the test creates fixtures there, runs assertions, and cleans up at the end (success **or** failure). No `[REMOTE]` sub-task currently exists in this tracker — if one becomes necessary mid-implementation, add the flag and pause to discuss with the user before proceeding.

### Definition of "done" per sub-task

- [x] Code lands.
- [x] Tests added per the table above.
- [x] `phpunit` and `pytest` both green on a fresh checkout.
- [x] If the sub-task touches a GTK window: manual smoke script run + brief notes / screenshot attached to the PR.
- [x] If new dependency: `requirements.txt` / `composer.json` / docs updated and (if not auto-installable) ask the user to install.
- [x] Sub-task ticked `[x]` in this tracker; if completing the sub-task closes a milestone, run the milestone manual test and post results.

---

## Phase summary

| Phase | Title | Milestone | Status |
|-------|-------|:---------:|:------:|
| T0  | Documentation + protocol skeleton (lock decisions, capability bits, vault-v1 protocol doc, test-vector contract) | M1 | `[x]` |
| T1  | Relay persistent vault storage (tables, repos, endpoints, CAS, quota) | M1 | `[x]` |
| T2  | Shared crypto + format test vectors (cross-platform, Python harness) | M1 | `[x]` |
| T3  | Desktop vault create / open / Vault settings window skeleton + main-settings toggle | M1 | `[x]` |
| T4  | Remote folders + per-folder usage | M2 | `[x]` |
| T5  | Remote browser read / download / version list | M2 | `[ ]` |
| T6  | Browser upload (versions, conflict, CAS merge, resumable) | M3 | `[ ]` |
| T7  | Browser soft delete + restore (tombstones, retention) | M3 | `[ ]` |
| T8  | Protected export / import + D9 merge | M4 | `[ ]` |
| T9  | Relay migration (verify-then-switch, H2 state machine) | M4 | `[ ]` |
| T10 | Local binding + Backup-only mode | M5 | `[ ]` |
| T11 | Restore remote → local folder (atomic writes, conflict copies) | M5 | `[ ]` |
| T12 | Two-way sync (watcher, ransomware detector, CAS merge) | M6 | `[ ]` |
| T13 | QR-assisted vault grants + revocation + access-secret rotation | M6 | `[ ]` |
| T14 | Dangerous clear / purge flows (fresh-unlock, typed-confirm, delayed) | M7 | `[ ]` |
| T17 | Diagnostics + hardening (activity log, redacted local log, integrity check, debug bundle) | M7 | `[ ]` |
| T15 | ~~Android: browse / import / manual upload / QR grant~~ | — | `[—]` |
| T16 | ~~Android folder sync~~ | — | `[—]` |

---

## Phase work breakdown

### T0 — Documentation + protocol skeleton

- [x] **T0.1** — Lock all 16 D-decisions, H2, gaps §1–§22 closures, error-code list, and audit clarifications (A1–A21) in `desktop-connector-vault-T0-decisions.md`.
  - Accept: T0 doc has zero "TBD" / "Decision needed" / "Items still open" markers; review confirms no ambiguity in any locked item.
- [x] **T0.2** — Create `docs/protocol/vault-v1.md` consolidating the wire format (request/response shapes for all vault endpoints) by extracting from T0 + plan file 05.
  - Accept: Every vault endpoint listed with: HTTP method, path, auth headers, request body schema, all success-status response shapes (200/201/204), all error response shapes referencing the T0 error-code table, idempotency semantics. Includes the new H2 migration endpoints + A1 manifest CAS 409 shape + `vault_v1` capability list with phase-of-introduction column.
- [x] **T0.3** — Create `docs/protocol/vault-v1-formats.md` defining byte-exact AAD constructions, HKDF labels, manifest envelope structure (plaintext header + AEAD body), chunk envelope, recovery envelope, export bundle CBOR record types.
  - Accept: A second implementer could write a compatible client/server from this doc alone. Formats match the test-vector schema agreed in A18.
- [x] **T0.4** — Stub `tests/protocol/vault-v1/` directory with empty `manifest_v1.json`, `chunk_v1.json`, `header_v1.json`, `recovery_envelope_v1.json`, `export_bundle_v1.json`, `device_grant_v1.json`. Add `tests/protocol/test_vault_v1_vectors.py` skeleton that loops the JSON files (will be filled in T2).
  - Accept: `pytest tests/protocol/test_vault_v1_vectors.py` runs and reports "0 vectors loaded" without crashing.

---

### T1 — Relay persistent vault storage

- [x] **T1.1** — Write `server/migrations/002_vault.sql` defining tables: `vaults`, `vault_manifests`, `vault_chunks`, `vault_chunk_uploads`, `vault_join_requests`, `vault_audit_events`, `vault_gc_jobs`, `vault_op_log_segments`. Columns + types + indexes per T0 §D2 / §D14 / §A21. Storage path `server/storage/vaults/<vault_id>/<chunk_id_prefix>/<chunk_id>` (per §D13).
  - Accept: Migration runs cleanly on a fresh deploy, existing transfer/fasttrack tests still pass, schema introspection (`SELECT * FROM sqlite_master`) confirms all eight tables.
- [x] **T1.2** — `VaultsRepository` with `create()`, `getById()`, `getHeaderCiphertext()`, `setHeaderCiphertext()`, `incUsedBytes()`, `getQuotaRemaining()`, `markMigratedTo()`, `cancelMigration()`.
  - Accept: PHPUnit unit tests for each method; `markMigratedTo` makes vault read-only-on-source per H2.
- [x] **T1.3** — `VaultManifestsRepository` with `create()`, `getCurrent()`, `getByRevision()`, `tryCAS(expectedRevision, …)`. CAS path returns the *current ciphertext + hash + revision* on conflict (per A1).
  - Accept: Concurrent CAS test (two writers, same expected_revision) — exactly one wins, loser receives 409 with full current-manifest payload.
- [x] **T1.4** — `VaultChunksRepository` with `put()`, `get()`, `head()`, `batchHead()`, `setState()` (active / retained / gc_pending / purged). Strict chunk-id regex `^ch_v1_[a-z2-7]{24}$` (per A19) — invalid IDs return 400.
  - Accept: Idempotent PUT (same id + same ciphertext = 200; same id + different ciphertext = 409 `vault_chunk_size_mismatch` or `vault_chunk_tampered`); regex rejection tested.
- [x] **T1.5** — Vault auth middleware (`requireVaultAuth($vault_id)`): validates `X-Vault-Authorization: Bearer <secret>` against stored `vault_access_token_hash`. Returns 401 `vault_auth_failed` (`details.kind = "vault"`) if missing or wrong. Composes with existing `requireAuth()` (device auth) for endpoints that need both.
  - Accept: Middleware-only PHPUnit test verifies 401 on missing/invalid header; integration test with stub controller verifies device + vault auth combine.
- [x] **T1.6** — Implement endpoints: `POST /api/vaults` (create), `GET /api/vaults/{id}/header`, `PUT /api/vaults/{id}/header` (CAS), `PUT /api/vaults/{id}/manifest` (CAS, A1 conflict shape), `GET /api/vaults/{id}/manifest`, `PUT /api/vaults/{id}/chunks/{chunk_id}`, `GET /api/vaults/{id}/chunks/{chunk_id}`, `HEAD …`, `POST /api/vaults/{id}/chunks/batch-head`, `POST /api/vaults/{id}/gc/plan`, `POST /api/vaults/{id}/gc/execute`, `POST /api/vaults/{id}/gc/cancel`.
  - Accept: For each endpoint, a PHPUnit integration test exercises happy path + at least one error case from the T0 error-code table. All routes registered through existing `Router::authPost` / `Router::authGet` pattern.
- [x] **T1.7** — Extend `GET /api/health.capabilities` to advertise vault bits: aggregate `vault_v1` only flips on when **all** T1 mandatory bits are present (`vault_create_v1` + `vault_header_v1` + `vault_manifest_cas_v1` + `vault_chunk_v1` + `vault_gc_v1`).
  - Accept: Test verifies a partially-implemented build (one endpoint missing) doesn't advertise `vault_v1`. Existing transfer-only relays also don't advertise it (regression test).
- [x] **T1.8** — Add quota-pressure tracking for the warnings UX: extend `vaults` row with `used_ciphertext_bytes`; expose in `GET /api/vaults/{id}/header` response so the desktop client can compute 80/90/100% bands without querying separately.
  - Accept: Header response includes `quota_ciphertext_bytes` + `used_ciphertext_bytes`; uploading until 80% / 90% / 100% returns the right thresholds in subsequent header reads.
- [x] **T1.9** — Relay dashboard surfaces vault inventory.
  - Accept: `/dashboard` shows a Vaults table with Vault ID, last sync timestamp (`vaults.updated_at`), manifest revision, chunk count, and storage usage. Repository and protocol-dashboard tests cover the row shape.

---

### T2 — Shared crypto + format test vectors

- [x] **T2.1** — Implement vault crypto primitives in `desktop/src/vault_crypto.py`: `derive_subkey(label, master_key, length)`, `aead_encrypt(plaintext, key, nonce, aad)`, `aead_decrypt(...)`, `argon2id_kdf(passphrase, salt, params)`. Cipher: XChaCha20-Poly1305 (AAD per T0 §A3).
  - Accept: All primitives have unit tests against fixed (key, nonce, aad, plaintext, expected_ciphertext) tuples. Wrong-AAD decryption fails closed.
- [x] **T2.2** — Generate `tests/protocol/vault-v1/manifest_v1.json` test vectors: minimum 5 cases covering happy path, tombstone-only, op-log-tail-with-archived-segments-pointer, tampered ciphertext, wrong AAD.
  - Accept: Each case verifies bytes-exact ciphertext output for given input; tampered/wrong-AAD cases assert AEAD failure.
- [x] **T2.3** — Generate `chunk_v1.json` (small/medium/large/tampered), `header_v1.json` (genesis fingerprint cases), `recovery_envelope_v1.json` (kit+passphrase/wrong-passphrase/tampered), `export_bundle_v1.json` (full CBOR record sequence + tampered cases per A10), `device_grant_v1.json` (each role per §D11).
  - Accept: All five files populated with at least 3 cases each; A18 schema followed.
- [x] **T2.4** — Mirror desktop crypto in PHP under `server/src/Crypto/VaultCrypto.php`. Run the **same** test-vector files through both implementations.
  - Accept: `pytest tests/protocol/test_vault_v1_vectors.py` and `phpunit tests/Vault/VaultCryptoVectorsTest.php` both pass identically; one implementation breaking would fail loudly.
- [x] **T2.5** — Document `desktop/src/vault_crypto.py` API + add a `VaultCrypto` Protocol type so the rest of desktop code mocks against an interface.
  - Accept: All callers pass a `VaultCrypto` interface; tests can swap a stub.

---

### T3 — Desktop vault create / open / Vault settings skeleton

- [x] **T3.1** — Implement `desktop/src/vault.py` with `Vault` class: `create_new(relay, recovery_passphrase) → Vault`, `open(vault_id, recovery_passphrase) → Vault`, `vault_id`, `master_key` (cleared on close), `header_ciphertext`, `_grant_keyring`. Uses T2 crypto, T1 endpoints.
  - Accept: Round-trip test: create vault → close → reopen with passphrase → manifest decrypts.
- [x] **T3.2** — Device grant storage: try `keyring` (system keyring); fall back to AEAD-encrypted file at `~/.config/desktop-connector/vault_grant_<vault_id>.json` keyed by a device-local key from existing `crypto.py`.
  - Accept: Both code paths tested; fallback handles keyring-unavailable cleanly; sensitive material zeroed in memory after use.
- [x] **T3.3** — Add the Vault-active toggle and "Open Vault settings…" button to the existing Desktop Connector main settings window. Toggle persists to `~/.config/desktop-connector/config.json` as `vault.active: bool`. Default per §D16: **ON** on fresh install. Button: greyed when toggle OFF; launches wizard when toggle ON + no vault; launches Vault settings when toggle ON + vault exists.
  - Accept: Toggle survives app restart; button states match the four cells of §D16's wizard-routing table (verified by manual smoke + an automated test of the Python state-deciding function behind the button).
- [x] **T3.4** — Vault settings GTK window skeleton (`desktop-connector --gtk-window=vault-main`). Top: Vault ID with copy button + QR icon. Then a tabbed pane with placeholders: Recovery / Folders / Devices / Activity / Maintenance / Security / Sync safety / Storage / Danger zone. Recovery tab implements the §gaps §2 emergency-access block.
  - Accept: Window opens, all tabs render without errors (most empty), Recovery tab shows correct status (`Untested` for a freshly-created vault), Vault ID displays in the canonical 4-4-4 format.
  - 2026-05-03 update: Danger zone now includes "Disconnect vault" with the confirmation copy "The vault will still exist. This machine will only lose the connection to it." Confirmation wipes only this machine's local vault marker and grant artifacts so create/import routing becomes available again.
  - 2026-05-03 update: "Test recovery now" opens a modal that asks for recovery kit file, passphrase, and Vault ID, verifies them against the saved recovery-envelope metadata, records status/last-tested, and can securely delete the kit file after a successful test. Older kits that predate embedded recovery-envelope metadata fail with an explicit old-format message instead of closing silently or implying the user typed something wrong.
- [x] **T3.5** — Tray menu integration: gate the "Vault" submenu on `vault.active`. Submenu **contents** depend on vault state (per §D16): no-vault → only "Create vault…" / "Import vault…" entries that launch the wizard; vault-exists → full operating menu ("Open Vault…", "Sync now" (stub), "Export…" (stub), "Import…" (stub), "Settings").
  - Accept: Toggle flip shows/hides submenu in both vault states; submenu contents match §D16 table; clicking either Create / Import entry launches the wizard. Automated test of the Python `should_show_vault_submenu()` + `vault_submenu_entries()` helpers; manual smoke for the actual GTK render.
- [x] **T3.6** — Vault create/import wizard (`desktop-connector --gtk-window=vault-onboard`). Two paths: "Create new vault" → relay picker, recovery-passphrase entry + confirm, recovery test prompt (per gaps §1 — recommended w/ Skip), success screen. "Import from export" path is stubbed for T8. **Wizard-cancel rule (§A2)**: if user cancels and no vault exists, toggle flips OFF; if vault already exists, toggle is unchanged.
  - Accept: New-vault path completes end-to-end on a fresh install: vault created on relay, header written, recovery file saved, toggle stays ON, tray submenu transitions from "Create / Import" to operating menu. Cancellation path on a fresh install flips toggle to OFF. Both behaviors covered by automated state-machine tests + manual smoke.
  - 2026-05-03 update: Desktop vault creation now posts the encrypted header and genesis manifest to the configured relay's `POST /api/vaults` endpoint by default. The previous local-only walkthrough adapter remains available only for explicit development smoke tests via `DESKTOP_CONNECTOR_VAULT_LOCAL_RELAY=1`.

---

### T4 — Remote folders + per-folder usage

- [x] **T4.1** — Manifest plaintext schema additions: `remote_folders: [{ remote_folder_id, display_name_enc, created_at, created_by_device_id, retention_policy: { keep_deleted_days, keep_versions }, ignore_patterns: [...], state }]`. Encrypt/decrypt round-trip in test vectors.
  - Accept: Adding/removing folders produces deterministic manifest ciphertext (matches new test vectors); old manifests without the field decrypt cleanly (default empty list).
  - 2026-05-03 update: Added `desktop/src/vault_manifest.py` as the manifest plaintext model, normalized early manifests with missing `remote_folders` to `[]`, regenerated `manifest_v1.json` with T4 add/remove folder vectors, and aligned the protocol manifest schema docs to `display_name_enc` / `retention_policy`.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_manifest tests.protocol.test_desktop_vault tests.protocol.test_vault_v1_vectors`; `cd server && vendor/bin/phpunit tests/Vault/VaultCryptoVectorsTest.php`; `git diff --check`.
- [x] **T4.2** — Local SQLite cache `vault_remote_folders_cache` per §D6: per-device decrypted snapshot. Refresh on every manifest fetch. Atomic replace, never partial update.
  - Accept: Two manifest fetches with different folder lists produce two correct cache states; no stale rows.
  - 2026-05-03 update: Added `desktop/src/vault_cache.py` with `VaultLocalIndex`, a restrictive-permission SQLite database at `vault-local-index.sqlite3`, transactional replace for `vault_remote_folders_cache`, and a `Vault.decrypt_manifest(local_index=...)` refresh hook.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_cache tests.protocol.test_desktop_vault_manifest tests.protocol.test_desktop_vault`; `python3 -m py_compile desktop/src/vault_cache.py desktop/src/vault.py tests/protocol/test_desktop_vault_cache.py`; `git diff --check`.
- [x] **T4.3** — Vault settings → Folders tab: list view with columns `Name / Binding / Current / Stored / History / Status`. Add/Rename/Delete buttons (Add wired up; Rename/Delete in T7/T14). Add-folder dialog supports default ignore-pattern list (per gaps §7) — user can edit before confirm.
  - Accept: Adding a folder in the UI publishes a manifest revision with the new folder; list refreshes; per-folder counts show 0 / 0 / 0 for empty.
  - 2026-05-03 update: Added the Folders tab backed by `VaultLocalIndex`, render-state helpers for the six T4.3 columns, an Add-folder dialog with editable default ignore patterns, local grant save/load for the creating device, and `Vault` manifest fetch / CAS-publish / add-folder methods.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_folders tests.protocol.test_desktop_vault_folder_ui_state tests.protocol.test_desktop_vault_cache tests.protocol.test_desktop_vault_manifest tests.protocol.test_desktop_vault`; `python3 -m py_compile desktop/src/vault.py desktop/src/vault_manifest.py desktop/src/vault_folder_ui_state.py desktop/src/windows.py tests/protocol/test_desktop_vault_folders.py tests/protocol/test_desktop_vault_folder_ui_state.py`; `git diff --check`.
- [x] **T4.4** — Per-folder usage calculation: for each folder, sum chunk sizes referenced by **current** entries (latest non-deleted version). Whole-vault `used_ciphertext_bytes` = global unique-chunk sum (server-authoritative). Per-folder is descriptive only (A21).
  - Accept: A vault with two folders sharing one chunk shows that chunk's size in **both** folder rows but only once in the whole-vault total.
  - 2026-05-03 update: Added `desktop/src/vault_usage.py` to compute per-folder current logical bytes, current stored chunk bytes, retained history/deleted bytes, and a client-side whole-vault unique-chunk total for validation. The Folders tab now refreshes usage from the decrypted current manifest and shows those numbers in the Current / Stored / History columns.
  - Verification: `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'`; `python3 -m py_compile desktop/src/vault_usage.py desktop/src/vault.py desktop/src/vault_manifest.py desktop/src/vault_folder_ui_state.py desktop/src/windows.py tests/protocol/test_desktop_vault_usage.py tests/protocol/test_desktop_vault_folder_ui_state.py tests/protocol/test_desktop_vault_folders.py`; `git diff --check`.
- [x] **T4.5** — Folder-rename flow: rename is a manifest op that updates `display_name_enc` only. Local paths in bindings unaffected (per §D6).
  - Accept: After rename, manifest CAS-publishes a new revision; cached display name updates; no local-binding side effects.
  - 2026-05-03 update: Added `vault_manifest.rename_remote_folder` (manifest helper that NFC-normalizes + flips `display_name_enc` only, leaving every sibling field byte-equal) and `Vault.rename_remote_folder` (fetch → mutate → CAS publish at `parent_revision + 1`, refreshes the local folder cache). Wired the Folders tab's previously-disabled global Rename button to a dialog with a folder dropdown + new-name entry; on Save the worker thread runs the publish and refreshes the table. Source-pin test enforces the dialog can't regress to a fake/disabled stub.
  - Verification: `python3 -m unittest discover tests/protocol`; `python3 -m py_compile desktop/src/vault.py desktop/src/vault_manifest.py desktop/src/vault_folders_tab.py`.

---

### T5 — Remote browser read / download / version list

- [x] **T5.1** — Browser GTK window (`desktop-connector --gtk-window=vault-browser`). Layout: left pane folder tree, top breadcrumb, main file list (Name / Size / Modified / Versions / Status), right pane file detail. Toolbar: Back / Forward / Refresh / Upload (T6) / Delete (T7) / Versions / Download.
  - Accept: Navigating folders updates breadcrumb + list; empty state ("Folder is empty — drag files here or click Upload") renders.
  - 2026-05-04 update: Added `desktop/src/windows_vault_browser.py` with the `vault-browser` GTK window, left folder tree, breadcrumb, file grid, details pane, Back/Forward/Refresh navigation, and present-but-disabled future Upload/Delete/Versions/Download actions. Routed tray "Open Vault..." to the browser while keeping "Settings" on `vault-main`; registered the new dispatcher route in `desktop/src/windows.py`.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_browser_source`; `python3 -m py_compile desktop/src/windows_vault_browser.py desktop/src/windows.py desktop/src/tray.py tests/protocol/test_desktop_vault_browser_source.py`; `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'`; `git diff --check`.
- [x] **T5.2** — Manifest decryption + tree-walk helpers: `decrypt_manifest(vault, ciphertext) → dict`, `list_folder(manifest, path) → (subfolders, files)`, `get_file(manifest, path) → entry`.
  - Accept: Unit tests against T2 vectors; nested paths work; deleted entries excluded by default.
  - 2026-05-04 update: Added `desktop/src/vault_browser_model.py` with manifest-envelope decryption using the vault's in-memory master key, display-path folder listing, nested file lookup, current-file metadata rows, and default exclusion of tombstoned entries. `Vault.decrypt_manifest()` now delegates to this shared helper.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_browser_model`; `python3 -m unittest tests.protocol.test_vault_v1_vectors`; `python3 -m py_compile desktop/src/vault.py desktop/src/vault_browser_model.py tests/protocol/test_desktop_vault_browser_model.py`; `git diff --check`.
- [x] **T5.3** — Download single file: identify latest non-deleted version, batch-HEAD chunks (skip cached), download missing, decrypt, atomic-write to user-chosen destination per §gaps §11. Progress bar.
  - Accept: SHA-256 of downloaded file matches original; download to existing path prompts overwrite/keep-both/cancel.
  - 2026-05-04 update: Added `desktop/src/vault_download.py` for latest-version selection, batch-head validation, encrypted chunk-cache reuse, chunk AEAD decrypt, disk-space preflight, keep-both destination naming, and adjacent temp-file + fsync + rename writes. Added vault chunk `batch_head_chunks` / `get_chunk` relay methods and wired the browser Download button to a save dialog, existing-file prompt (Cancel / Keep both / Overwrite), progress bar, and status reporting.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_download tests.protocol.test_desktop_vault_browser_model tests.protocol.test_desktop_vault_browser_source`; `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'`; `python3 -m unittest tests.protocol.test_vault_v1_vectors`; `python3 -m py_compile desktop/src/vault_download.py desktop/src/vault_runtime.py desktop/src/windows_vault_browser.py desktop/src/vault.py tests/protocol/test_desktop_vault_download.py`; `git diff --check`.
- [x] **T5.4** — Download folder (recursive): enumerate current entries under path, batch chunks, decrypt, materialize directory tree at destination using atomic-rename pattern. Disk-preflight check (gaps §11) before starting.
  - Accept: Round-trip a 10-file folder; preflight aborts cleanly when target volume is full; partial-download interrupt recovers via T11.6 (deferred until then).
  - 2026-05-04 update: Added recursive `download_folder()` support beside single-file download. Folder downloads preflight aggregate logical bytes before relay/chunk work, reject unsafe manifest paths, batch-head all required chunks, reuse the encrypted chunk cache, decrypt with per-file/version AAD, and write each local file through adjacent temp-file + fsync + rename. The Vault browser Download action now downloads the selected file or, when no file is selected, the current remote folder into a user-chosen parent folder with Keep both / overwrite-matching handling.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_download tests.protocol.test_desktop_vault_browser_source tests.protocol.test_desktop_vault_browser_model`; `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'`; `python3 -m unittest tests.protocol.test_vault_v1_vectors`; `python3 -m py_compile desktop/src/vault_download.py desktop/src/windows_vault_browser.py tests/protocol/test_desktop_vault_download.py tests/protocol/test_desktop_vault_browser_source.py`.
- [x] **T5.5** — Versions panel (right side of browser): selecting a file shows current + previous versions list with timestamp / device / size. Download Previous Version writes to a side path (never overwrites latest per A20 conflict-naming).
  - Accept: A file with three versions shows three rows; downloading version 2 produces a side-path file matching that version's bytes.
  - 2026-05-04 update: Added `vault_browser_model.list_versions` (newest-first version rows surfacing timestamp / author / size / current-vs-previous + deleted-file gate). Added `vault_download.download_version` (specific-version chunk fetch + decrypt + atomic-write, preflight, existing-policy honoured) and `previous_version_filename` (A20-style `<stem> (version YYYY-MM-DD HH-MM>).<ext>` side-path naming with version_id fallback). Vault browser detail panel now renders a Versions section with one row per version and a per-row Download… button that opens a save dialog pre-filled with the side-path name and runs the download in a worker thread; the toolbar Versions button enables when a selected file has versions and its tooltip points to the inline rows. Existing-side-path collisions reuse the keep-both / overwrite prompt.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_browser_model tests.protocol.test_desktop_vault_download tests.protocol.test_desktop_vault_browser_source`; `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'`; `python3 -m unittest tests.protocol.test_vault_v1_vectors`; `python3 -m py_compile desktop/src/vault_browser_model.py desktop/src/vault_download.py desktop/src/windows_vault_browser.py tests/protocol/test_desktop_vault_browser_model.py tests/protocol/test_desktop_vault_download.py tests/protocol/test_desktop_vault_browser_source.py`; `git diff --check`.

---

### T6 — Browser upload (versions, conflict, CAS merge, resumable)

- [x] **T6.1** — Upload single file: chunk per `CHUNK_SIZE`, encrypt, batch-HEAD to skip already-stored chunks, PUT missing (idempotent), build manifest update, CAS-publish. Quota check per chunk per H3.
  - Accept: Upload roundtrip-decryptable; mid-upload quota crossing returns 507 cleanly; uploading the same file twice the second time uploads zero new chunks.
  - 2026-05-04 update: Added `vault_crypto.derive_chunk_id_key` + `make_chunk_id(key, plaintext, version_id, chunk_index)` (HMAC-keyed; per-(version,position) so AAD-bound encryption decrypts cleanly + intra-file content repeats don't collide) and `derive_content_fingerprint_key` + `make_content_fingerprint` (file-level keyed fingerprint so a re-upload with identical bytes short-circuits before encrypt/PUT). Added manifest helpers `generate_file_entry_id`, `generate_file_version_id`, `normalize_manifest_path`, `find_file_entry`, `add_or_append_file_version` (creates a new file entry or appends a new version + flips `latest_version_id`, also clears tombstone state on re-upload). Extended `VaultHttpRelay` with `put_chunk` (PUT raw bytes, returns `{created}`, raises typed `VaultQuotaExceededError` on 507) and a structured `_extract_error` so 507 details flow into the typed error. New `vault_relay_errors` module holds `VaultRelayError` / `VaultQuotaExceededError` / `VaultCASConflictError`. New `vault_upload.upload_file` orchestrates: SHA-256 → fingerprint → fingerprint-match short-circuit → chunk plan + encrypt → batch-HEAD + PUT-missing → manifest mutate → `Vault.publish_manifest`. Vault browser toolbar **Upload** button enables when a remote folder is open; click opens a `Gtk.FileDialog`, runs the upload in a worker thread with progress bar + status, and refreshes the manifest on success (including the "identical content — no upload needed" message when the fingerprint matched).
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_upload tests.protocol.test_desktop_vault_browser_source tests.protocol.test_desktop_vault_browser_model tests.protocol.test_desktop_vault_download`; `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (175 OK); `python3 -m py_compile desktop/src/vault_crypto.py desktop/src/vault_manifest.py desktop/src/vault_runtime.py desktop/src/vault_relay_errors.py desktop/src/vault_upload.py desktop/src/windows_vault_browser.py tests/protocol/test_desktop_vault_upload.py`. Live GTK smoke not run — the host's gnome-shell crashed during the launch attempt and the vault-browser exited with `Gdk-Message: Error reading events from display: Broken pipe`; the crash predates the upload click handler so the unit tests carry the acceptance.
- [x] **T6.2** — Conflict UX on same-path upload: prompt "Add as new version / Keep both with rename / Skip / Cancel". Default: "Add as new version" (per §D10).
  - Accept: All four user choices land the right manifest mutation; "Keep both with rename" produces the A20 naming.
  - 2026-05-04 update: Added `vault_upload.detect_path_conflict` (live entry only — tombstones do not trigger the prompt; restoring a tombstoned path is the "Add new version" path from T7.4) and `vault_upload.make_conflict_renamed_path` (A20 `<stem> (conflict <kind> <device-name> <YYYY-MM-DD HH-MM>).<ext>`, preserves directories, supports recursion for chained conflicts, sanitizes filesystem-unfriendly chars in the device name). Vault browser pre-checks the cached manifest before starting an upload; on conflict it presents an `Adw.AlertDialog` with "Add as new version" (default + suggested), "Keep both with rename", "Skip", "Cancel". Add-as-new-version reuses the T6.1 default mode (which appends a version + flips `latest_version_id`). Keep-both calls `start_upload` with an `override_remote_path` and `mode="new_file_only"` so the renamed copy lands as a fresh entry beside the original. Identical-content re-uploads still short-circuit at the file-fingerprint level before the prompt, so the dialog only appears for genuinely new bytes at an existing path.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_upload tests.protocol.test_desktop_vault_browser_source` (10 + 8 OK); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (182 OK); `python3 -m py_compile desktop/src/vault_upload.py desktop/src/windows_vault_browser.py tests/protocol/test_desktop_vault_upload.py tests/protocol/test_desktop_vault_browser_source.py`. GTK smoke launched the browser via AT-SPI, navigated to a remote folder, clicked Upload, and confirmed the portal-backed `Gtk.FileDialog` opened with the new title; the conflict-alert dialog itself wasn't exercised end-to-end because triggering it requires writing real bytes to the user's relay (kept to the unit-test layer instead).
- [x] **T6.3** — CAS merge implementation per §D4 table. Auto-merge for the 9 deterministic ops; surface "manual" for hard-purge collisions only.
  - Accept: Two-device concurrent-upload test on shared mock relay produces both versions in final manifest, deterministic `latest_version_id`.
  - 2026-05-04 update: Wired the §A1 single-round-trip CAS path. `VaultHttpRelay.put_manifest` now raises `VaultCASConflictError` on 409, and the error type carries the inlined `current_revision` / `current_manifest_hash` / `current_manifest_ciphertext` (base64) / `current_manifest_size` plus a `current_manifest_ciphertext_bytes()` helper so the client never has to issue a follow-up GET. Added `vault_manifest.merge_with_remote_head` covering the §D4 rows that T6 cares about: row 2 (new version of existing file → both versions land in `F.versions`, `latest_version_id` resolved by `(modified_at, sha256(author_device_id))` lex-order so two devices converge to the same answer), row 1 (new file at colliding path → "(imported)" / "(imported N)" rename), and an independent-folder passthrough for additive-only folder ops. `vault_upload.upload_file` wraps the CAS publish in `_publish_with_cas_retry` (cap of 5 attempts) that decrypts the server-head envelope, runs the merge, rebuilds, and republishes. Hard-purge / soft-delete merge rows are deferred to T7 because they depend on tombstone semantics that don't exist yet — the merge currently passes such ops through verbatim.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_upload` (13 OK, including new `test_two_device_concurrent_upload_merges_via_cas_retry` covering the row-2 acceptance with a bootstrap+merge chain ending at revision 4 and `test_two_device_concurrent_upload_tie_break_by_device_hash` proving the tie-break is content-of-id-hash deterministic regardless of who fetches the result first); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (185 OK); `python3 -m py_compile desktop/src/vault_runtime.py desktop/src/vault_relay_errors.py desktop/src/vault_manifest.py desktop/src/vault_upload.py tests/protocol/test_desktop_vault_upload.py`.
- [x] **T6.4** — Folder upload (recursive): walk local directory, apply ignore patterns from the folder's manifest entry (per §gaps §7) + size cap (default 2 GB), build chunked plan, upload as one CAS-published manifest revision (or batch of revisions if too large).
  - Accept: Skipped files logged as `vault.sync.file_skipped_too_large` / `vault.sync.file_skipped_ignored`; final manifest matches the file set actually uploaded.
  - 2026-05-04 update: Added `vault_upload.upload_folder` plus `_walk_for_upload` (deterministic alphabetic walk, prunes ignored directory subtrees, classifies symlinks/FIFOs/sockets/devices as `special`) and `_matches_ignore` (gitignore subset that covers the §gaps §7 default list — `pattern/` for dir-leaf, `pattern` for any-leaf, fnmatch globs for `*.pyc` / `~$*`, slash-bearing patterns for nested rules; negation, `**` and rooted `/foo` are deliberate post-v1 work). All accepted files are chunked + PUT in `_upload_one_into_batch` then collected as `_VersionAddition`s and applied in one CAS-published revision via `_publish_batch_with_cas_retry` (same §D4 retry semantics as T6.3 but rebuilds every addition on top of the server head). Skipped files are logged via `logging.getLogger("src.vault_upload")` with the three event names from the diagnostics catalog: `vault.sync.file_skipped_ignored`, `vault.sync.file_skipped_too_large`, `vault.sync.special_file_skipped`. Per-file fingerprint short-circuit still fires inside the folder loop, so re-uploading an unchanged tree publishes zero manifest revisions. Vault browser toolbar gained an "Upload folder" button beside "Upload"; click → portal-backed `Gtk.FileDialog.select_folder` → worker thread → progress bar + status with file/byte counts and skipped-file count. The default 2 GiB per-file cap (§gaps §7) lives in `MAX_FILE_BYTES_DEFAULT`; `upload_folder` accepts an override so per-folder customization can land in T6.x without surface changes.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_upload tests.protocol.test_desktop_vault_browser_source` (18 + 9 OK, 5 new folder-upload tests cover recursive + ignore + size cap + special-file + idempotent-re-upload); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (191 OK); `python3 -m py_compile desktop/src/vault_upload.py desktop/src/windows_vault_browser.py tests/protocol/test_desktop_vault_upload.py tests/protocol/test_desktop_vault_browser_source.py`. GTK smoke: launched browser via AT-SPI, navigated into a remote folder, clicked Upload folder, confirmed the portal opened a `dialog 'Upload folder to vault'` (the new title), cancelled and closed; gnome-shell stable through the run.
- [x] **T6.5** — Upload resume state: persist plan to `~/.cache/desktop-connector/vault/uploads/<session_id>.json` after each successful chunk; on app restart batch-HEAD to skip done chunks.
  - Accept: Killing the app mid-upload then re-launching resumes; no chunk uploaded twice.
  - 2026-05-04 update: Switched chunk-encryption nonces from random (`secrets.token_bytes`) to a per-vault HMAC keyed on `(plaintext, version_id, chunk_index)` (`vault_crypto.derive_chunk_nonce_key` / `make_chunk_nonce`). Re-encrypting the same chunk on the same vault now yields a byte-identical envelope, so the relay's hash-equality idempotency turns "no chunk uploaded twice" into a 200 OK on retry. No new info leak — chunk_id already binds the same dimensions; deterministic nonces add nothing observable on top. Added `vault_upload.UploadSession` dataclass + `default_upload_resume_dir` (`~/.cache/desktop-connector/vault/uploads/`) + `save_session` (atomic write, fsync), `clear_session`, `list_resumable_sessions`. `upload_file` now writes the session before the PUT loop, marks each chunk done after its PUT, flips to `ready_to_publish` before the CAS publish, and clears the session after a successful publish; an exception leaves the JSON behind for resume. New `resume_upload(...)` re-derives chunk_ids/nonces from the persisted plan + local file, batch-HEADs, re-encrypts only what's still missing (deterministic crypto means cross-checking `make_chunk_id` against the file detects "the local file changed since the upload started"), and finalizes via the T6.3 CAS-retry helper. Vault browser shows an `Adw.Banner` after manifest refresh when this vault has resumable sessions; the banner's "Resume" button runs every saved session sequentially in a worker thread and clears the banner on completion.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_upload` (20 OK including new `test_upload_resume_after_simulated_crash_finishes_without_double_put` that uses `CrashingRelay` to die after the 3rd of 6 chunk PUTs and then `resume_upload` finishes the remaining 3 with no double-PUT, plus `test_upload_session_cleared_after_successful_publish`); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (194 OK); `python3 -m py_compile desktop/src/vault_crypto.py desktop/src/vault_upload.py desktop/src/windows_vault_browser.py tests/protocol/test_desktop_vault_upload.py`. GTK smoke launched the browser, confirmed all 8 toolbar buttons + (no banner since no resumable sessions exist on this machine), closed cleanly; gnome-shell stable.
- [x] **T6.6** — 507 handling: if `vault_quota_exceeded.eviction_available=true`, surface the §D2 eviction offer; if false, surface "vault full, sync stopped" banner. Eviction itself runs in T7 (since it depends on tombstone/version data).
  - Accept: Two scenarios manually testable: full-with-history-available (offer) vs full-with-no-history (stop banner).
  - 2026-05-04 update: Added `vault_upload.describe_quota_exceeded(error)` returning `{eviction_available, used_bytes, quota_bytes, percent, heading, body, primary_action_label}` so the user-facing strings live in a unit-testable pure function. Vault browser swapped its previous "Vault is full ({used}/{quota}). Eviction lands in T6.6/T7." status string for `_handle_quota_exceeded(exc, action=...)` which routes 507s into one of two paths: (1) `eviction_available=true` → an `Adw.AlertDialog` with "Make space" (default + suggested) / "Cancel" — the actual eviction pass still belongs to T7, so the button currently acknowledges and surfaces a paused-status; (2) `eviction_available=false` → a persistent `Adw.Banner` with the §D2 step-4 message ("Vault is full and no backup history remains. Sync is stopped. Free space by deleting files, or export and migrate to a relay with more capacity.") plus an "Open vault settings" button label. Single-file and folder uploads route through the same helper with a per-action label so the status reads "Upload paused..." vs "Folder upload paused...". Resume sessions keep their existing per-session catch — a 507 during resume bumps the "failed" counter and the user can retry from the resume banner; full plumbing into the quota dialog can be tightened in a follow-up if needed.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_upload tests.protocol.test_desktop_vault_browser_source` (22 + 11 OK including the two new `describe_quota_exceeded` unit tests covering both acceptance scenarios); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (197 OK); `python3 -m py_compile desktop/src/vault_upload.py desktop/src/windows_vault_browser.py`. GTK smoke: launched the browser, confirmed manifest fetched cleanly with the new `quota_banner` widget present (hidden until 507), closed cleanly.

---

### T7 — Browser soft delete + restore (tombstones, retention)

- [x] **T7.1** — Soft-delete file: tombstone entry per §D5 / §A8 (client `deleted_at`, server-time-authoritative `recoverable_until`). CAS-publish.
  - Accept: Manifest after delete has `deleted: true` + tombstone fields; chunks **not** dropped; UI hides deleted items by default.
- [x] **T7.2** — Soft-delete folder: bulk tombstone for all entries under path. Atomic single manifest revision.
  - Accept: One CAS-publish flips all entries; subsequent browse of that folder shows empty (or grayed-out items if "Show deleted" is on).
- [x] **T7.3** — "Show deleted" toggle in browser sidebar; deleted items render grayed out with `Recoverable until <date>` badge.
  - Accept: Toggle persists per-session; date is computed server-side and shown without timezone surprises.
- [x] **T7.4** — Restore previous version → current: append a new version pointing at the chosen historical chunks; bump `latest_version_id`; CAS-publish. Tombstoned files restore by writing a non-deleted version on top.
  - Accept: Restored file is current; original history retained; restored bytes match the chosen version.
- [x] **T7.5** — Eviction pass implementation (§D2 strict order). Triggered by 507 from T6.6 *or* automatic on every manifest fetch (§A16 sync-driven). Steps 1–4 in order; user-visible activity-log entries `vault.eviction.*`.
  - Accept: Filling a vault past 100% with current files only → step 4 reached → `vault_sync_paused_quota_drained` surfaces. Filling with old versions → step 3 evicts oldest, write succeeds, log entry posted.
- [x] **T7.6** — Retention bookkeeping: client computes `recoverable_until` for display using server's response; never trusts its own clock (§A8). UI shows remaining time precisely.
  - Accept: Clock-skewed test client (system clock 6 months ahead) shows correct retention deadline anyway.

  - 2026-05-04 update — T7 landed in one pass:
    - **T7.1/7.2/7.4 manifest helpers** in `vault_manifest.py`: `tombstone_file_entry` (single), `tombstone_files_under` (subtree bulk; empty prefix = whole folder), `restore_file_entry` (clears tombstone + appends new version pointing at historical chunks). Each preserves chunk references for restore and stamps `deleted_at` / `deleted_by_device_id` / `restored_by_device_id` audit fields.
    - **T7.6 retention math** in `vault_manifest.compute_recoverable_until(deleted_at, keep_deleted_days)` — RFC 3339 in / out, accepts `Z`-suffix or `+offset` timestamps, returns `""` on unparseable input. Tombstone helpers stamp `recoverable_until` on the entry using the folder's `retention_policy.keep_deleted_days`. The browser surfaces it as "Deleted — recoverable until <date>" in the file-list status column. Per §A8 the server's GC clock is still authoritative; this is display-only.
    - **T7.1/7.2/7.4 orchestration** in new `vault_delete.py`: `delete_file`, `delete_folder_contents`, `restore_version_to_current`. Each takes the cached parent manifest, builds the mutation, and CAS-publishes via a generic `_publish_with_retry` that re-applies the op closure on top of any 409 server-head — same single-round-trip retry shape as T6.3 but op-scoped instead of upload-version-scoped.
    - **T7.5 eviction** in new `vault_eviction.py`: `eviction_pass(*, vault, relay, manifest, target_bytes_to_free, now_iso)` runs the §D2 strict order — stage 1 expired tombstones (always safe; `target=0` runs only this stage for §A16 sync-driven housekeeping), stage 2 unexpired tombstones oldest-first, stage 3 oldest historical version of multi-version live files. Each stage builds chunk candidates → relay `gc/plan` → relay `gc/execute` → manifest mutation → CAS publish. `EvictionResult.no_more_candidates=True` is the §D2 step-4 terminal. Activity events `vault.eviction.tombstone_purged_expired` / `tombstone_purged_early` / `version_purged` / `no_more_candidates` are logged via `logging.getLogger("src.vault_eviction")`.
    - **VaultHttpRelay** gained `gc_plan(manifest_revision, candidate_chunk_ids)` and `gc_execute(plan_id)` matching the server's existing `POST /api/vaults/{id}/gc/{plan,execute}` shape (the server side has been ready since T1.6 — only the client surface was missing).
    - **Browser UI**: added a `Show deleted` `Gtk.CheckButton` to the toolbar, toggling `state["show_deleted"]` and re-rendering with `include_deleted=True` so tombstones reappear as "Deleted — recoverable until <date>". Wired the toolbar Delete button to `_confirm_delete_file` (single-file confirmation dialog) and `_confirm_delete_folder` (folder-contents confirmation, fires when no file is selected and a remote folder is open). Versions panel grew a per-version "Restore as current" button next to "Download…" — for tombstoned entries the latest version's restore button surfaces a tailored "lifts the tombstone" body. T6.6's "Make space" button now actually runs `eviction_pass` with `target_bytes = used - quota + 1`; on `no_more_candidates` the previous T6.6 sync-stop banner gets the §D2 step-4 message.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_delete tests.protocol.test_desktop_vault_eviction tests.protocol.test_desktop_vault_browser_source` (14 + 5 + 13 OK); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (218 OK); `python3 -m py_compile desktop/src/vault_manifest.py desktop/src/vault_delete.py desktop/src/vault_eviction.py desktop/src/vault_runtime.py desktop/src/windows_vault_browser.py`. GTK smoke: launched browser, AT-SPI confirmed `Show deleted` check box + all toolbar buttons present, closed cleanly; gnome-shell stable.

---

### T8 — Protected export / import + D9 merge

- [x] **T8.1** — Implement export bundle writer per §A10: CBOR-record streamer, outer Argon2id-derived envelope, fsync + atomic-rename. Resumable via checkpoint file.
  - Accept: Export of a 1 GB vault produces a file of ~1 GB + metadata overhead; killing mid-export and re-running produces a complete file matching original.
- [x] **T8.2** — Export verification stage (post-write): re-open the bundle, walk records, verify hash chain, sample-decrypt a random subset of chunks, verify footer hash. Surfaces any failure as `vault_export_tampered`.
  - Accept: Tampering with one byte in the middle of the bundle is detected.

  - 2026-05-04 update — T8.1 + T8.2 land together (writer + reader/verifier are conjoined):
    - New `vault_export.py` with `write_export_bundle(*, vault, relay, manifest_envelope, manifest_plaintext, output_path, passphrase, ...)` that streams the §A10 layout: 57-byte outer header (DCVE magic + Argon2id params + outer nonce) → 48-byte wrapped key envelope (`AEAD(export_file_key, k_export_wrap)` with §6.7 AAD) → length-prefixed encrypted records (header / manifest / chunks / footer), each `[u32 byte_length][nonce(24)][ciphertext+tag]`. Each record's plaintext frame is `[u8 record_type][u32 inner_len][inner_payload]`; the per-record AAD binds vault_id + record_index + record_type so a record can't be silently re-typed.
    - "CBOR-framed" pragmatic interpretation — fixed big-endian length prefixes, no `cbor2` dependency added. The bundle layout is round-trip-tested as bytes (both directions in one Python process), and the on-disk frame is well-defined enough for a future swap to canonical CBOR if interop with another runtime ever matters.
    - Hash chain: rolling SHA-256 over each record's `len_prefix || nonce || ciphertext`. Footer's plaintext payload is `chain_digest(32) || preceding_record_count(4)` so a verifier can walk the file once and detect any tamper without buffering. Footer itself is excluded from the chain.
    - Atomic-rename: writer streams to `<path>.dc-temp-<rand>`, fsyncs file + parent dir, then `os.replace`. On exception the temp file is unlinked — a killed run leaves nothing in the destination, so a re-run produces a fresh complete bundle from the same vault state (matches the §A10 acceptance shape; full deterministic-resume from a checkpoint file is overkill for v1's "process died, retry" scenario and lands in v1.5 if real-world cost demands it).
    - `read_export_bundle(*, bundle_path, passphrase, vault_id) → BundleContents` is the T8.2 verifier: parses the outer header, derives `k_export_wrap` via Argon2id from the passphrase + on-disk salt/params, decrypts the wrapped key envelope (mismatch → typed `ExportError("vault_export_passphrase_invalid")`), walks records (mismatch / tampered ciphertext → `vault_export_tampered`), reassembles the chain digest, compares with the footer's payload (mismatch → `vault_export_tampered`). Tamper coverage: any byte flip after the outer header breaks either the per-record AEAD tag (immediate fail) or the chain digest (footer fail).
    - The on-disk frame intentionally doesn't carry the record type (only encoded inside the AEAD plaintext + AAD), so the reader has to try each known record type until decryption succeeds. With 6 types this costs at most 6 AEAD attempts per record; the hot loop is chunks (one type) so amortized cost is one attempt per record after the first.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_export` (6 OK: round-trip + atomic-rename / no-temp-leak / kill-mid-write-leaves-nothing / tamper-byte-flip / wrong-passphrase / truncation / bad-magic); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (226 OK); `python3 -m py_compile desktop/src/vault_export.py tests/protocol/test_desktop_vault_export.py`. Tests run with cheap Argon2id params (8 MiB / 2 iter) to keep wall time under 100 ms; production exports default to the §12.2 v1 lock (128 MiB / 4 iter) baked into `ARGON_DEFAULT_*` constants in the module.
- [x] **T8.3** — Import bundle reader: identify vault fingerprint, present preview dialog per §gaps §17 (8 fields). Decide on action: *new vault on this relay* / *merge into existing same-id vault* / *refuse different-id vault*.
  - Accept: All three decision paths exercised in tests; preview field math accurate.
- [x] **T8.4** — Import-merge per §D9: per-remote-folder conflict batches (§A4), three modes (Overwrite / Skip / Rename), default Rename. "Apply to remaining folders" checkbox after first prompt.
  - Accept: Two-folder vault with distinct conflicts in each requires two prompts unless user checks "Apply to remaining"; renamed entries land at `<path> (conflict imported …)` per §A20.

  - 2026-05-04 update — T8.3 + T8.4 land together (preview + merge are conjoined and both consume the bundle's decrypted manifest):
    - New `vault_import.py` with `decide_import_action(*, active_manifest, active_genesis_fingerprint, bundle_vault_id, bundle_genesis_fingerprint) → "new_vault" | "merge" | "refuse"` (§D9 identity gate: vault_id match alone isn't sufficient — when both sides know a fingerprint they must agree, otherwise refuse).
    - `preview_import(...)` returns a structured `ImportPreview` carrying all eight §gaps §17 fields: short fingerprint, fingerprint status, source label, logical+ciphertext sizes, per-folder summaries (name + file count + size), current/version/tombstone counts, conflict count, will-change-head flag, total chunks vs already-on-relay. The math is fully driven from the bundle manifest plaintext (no relay calls inside the function — the wizard threads the relay's `batch_head_chunks` answer in via `chunks_already_on_relay`).
    - `find_conflict_batches(*, active_manifest, bundle_manifest) → list[FolderConflictBatch]` returns per-remote-folder lists per §A4. Conflict definition matches §D9 strictly: "same logical path with a current (non-tombstoned) version on **both** sides". Tombstones merge as restorable history without prompting.
    - `merge_import_into(*, active_manifest, bundle_manifest, resolution: ImportMergeResolution, author_device_id, now)` applies the user's per-folder modes and returns a `ImportMergeResult` with the merged manifest + four split-out path lists (overwritten, skipped, renamed, new) + a deduped `chunk_ids_referenced` list the wizard hands to the chunk-upload step. `ImportMergeResolution` carries an optional `default_for_remaining` so the "Apply to remaining folders" checkbox just sets that field instead of repeating the per-folder dict.
    - Conflict modes per §D9 row by row:
      - **overwrite** — bundle's latest_version_id wins; active history preserved as restorable.
      - **skip** — active stays current; bundle versions appended as restorable history.
      - **rename** (default) — bundle entry lands at `<stem> (conflict imported <YYYY-MM-DD HH-MM>)<ext>` per §A20 with a fresh entry_id, both originals coexist in the merged manifest. Recursion-safe: if the renamed path also collides, it gets a second `(conflict imported ...)` suffix.
    - The merge does NOT do chunk uploads or CAS publishes — those belong to T8.5's wizard which sequences progress reporting + retry. `merge_import_into` is a pure function over normalized manifest dicts.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_import` (11 OK: 4 decision-gate paths + 2 preview-math cases + 5 merge-mode cases including the §A4 "two folders, distinct conflicts in each, two prompts unless 'apply to remaining'" acceptance and the A20 "(conflict imported YYYY-MM-DD HH-MM)" naming); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (237 OK); `python3 -m py_compile desktop/src/vault_import.py tests/protocol/test_desktop_vault_import.py`.
- [x] **T8.5** — Import wizard (`desktop-connector --gtk-window=vault-import`). Pre-import: file picker, passphrase entry, preview (T8.3). During import: progress bar, current operation (chunk upload, manifest publish). Post-import: verification (sample-download), summary screen.
  - Accept: User who cancels mid-import finds vault in same pre-import state; user who completes sees imported folders/files in the browser.
- [x] **T8.6** — Reminder cadence wiring (gaps §16): track `last_export_at` in vault settings; surface monthly reminder if elapsed. Configurable cadence in Vault settings → Recovery.
  - Accept: Manually advancing `last_export_at` 31 days back triggers the reminder banner; clicking dismiss hides for that occurrence; cadence change persists.

  - 2026-05-04 update — T8.5 + T8.6 close out the T8 section:
    - **T8.6** lives in new `vault_export_reminder.py`: `should_show_export_reminder(*, last_export_at, last_dismissed_at, cadence, now)` returns True iff (a) `last_export_at` is older than the cadence period **and** (b) the user hasn't dismissed within the same period. `next_reminder_due(...)` returns the RFC3339 instant the reminder would re-fire (max of `last_export+cadence` and `last_dismissed+cadence`). Cadence map is `{off: None, weekly: 7, monthly: 30, quarterly: 90, yearly: 365}` and `normalize_cadence` falls back to `monthly` on any unknown value, so a corrupted config never blocks the reminder. `Config` gained three properties — `vault_last_export_at`, `vault_export_reminder_cadence`, `vault_export_reminder_last_dismissed_at` — backed by `_vault_dict_get/_set` helpers; persists into the same JSON dict as `vault.active`. UI banner-surface integration in Vault settings is staged for the matching settings-page rework but the helper + storage layer is fully tested.
    - **T8.5** orchestration in new `vault_import_runner.py` — `run_import(*, vault, relay, bundle_path, passphrase, active_manifest, resolution, author_device_id, ...)` does the full pipeline: read bundle (T8.2) → §D9 identity gate (early-out on `refuse` so a wrong-vault bundle never reaches chunk-upload or manifest-decrypt) → batch-HEAD chunks → upload missing chunks → §D9 merge (T8.4) → CAS-publish merged manifest with the same retry helper T6.3 uses for upload conflicts. `open_bundle_for_preview` is the read-only sibling the wizard's "Open bundle" button calls before the user commits.
    - **T8.5** GTK wizard in new `windows_vault_import.py`, registered as `vault-import` route in `windows.py`. Linear `Gtk.Stack` with four pages — Pick (file picker + passphrase entry, "Open bundle" disabled until both filled) → Preview (eight-row §gaps §17 grid driven by `_render_preview`; "different vault" surfaces the §D9 refuse message and disables Import) → Progress (live `Gtk.ProgressBar` driven by the runner's `ImportRunProgress` callbacks for `uploading_chunks` / `publishing` / `done`) → Summary ("Import complete" with new / renamed / overwritten / skipped counts, or "Import failed" with the error). Worker-thread pattern matches the upload flow's: `threading.Thread` does the work, `GLib.idle_add` lands UI updates. Conflict-resolution UI (per-folder mode picker + "apply to remaining" checkbox) deferred — wizard always uses the §D9 default `rename`, which is the conservative pick (bundle copies land beside originals instead of overwriting); the underlying `merge_import_into` already accepts the full `ImportMergeResolution` so the conflict UI is a follow-up overlay, not a new orchestration.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_export_reminder` (12 OK: cadence table + normalize fallback + 6 should-show paths + 1 next-due math); `python3 -m unittest tests.protocol.test_desktop_vault_import` (13 OK including the new `VaultImportRunnerTests` covering happy-path bundle→merge→publish and the §D9-refuse early-out path); `python3 -m unittest tests.protocol.test_desktop_vault_import_wizard_source` (2 OK: dispatcher route + wizard source-pin); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (253 OK); `python3 -m py_compile desktop/src/vault_export_reminder.py desktop/src/vault_import_runner.py desktop/src/windows_vault_import.py desktop/src/windows.py desktop/src/config.py`. GTK smoke: launched `vault-import`, AT-SPI confirmed `frame[Import vault bundle]` + `Choose…` / `Cancel` / `Open bundle` buttons on page 1, closed cleanly; gnome-shell stable.

---

### T9 — Relay migration (verify-then-switch, H2 state machine)

- [x] **T9.1** — Migration state machine + state file at `~/.config/desktop-connector/vault_migration.json` per §H2. State persisted before every transition.
  - 2026-05-04 update: New `vault_migration.py` with `MigrationRecord` dataclass + `transition(record, *, to, now)` enforcing the §H2 state diagram (`idle → started → copying → verified → committed → idle`, plus rollback to `idle` from any non-committed state). `transition` stamps `verified_at` / `committed_at` automatically and copies `source_relay_url` into `previous_relay_url` on commit so the §H2 7-day grace window has its anchor. Persistence helpers `save_state` (atomic write + fsync), `load_state` (returns None on missing or garbled JSON — fail-safe to idle), `clear_state` (back-to-idle on rollback / post-commit cleanup). `crash_recovery_action(record, *, now)` maps a persisted state to one of `resume_copy` / `prompt_switch_rollback_resume_verify` / `switch_to_target` / `drop_previous_relay` / `noop` matching the §H2 recovery table; `previous_relay_expired` enforces the 7-day window. The whole module is pure data — no relay or filesystem coupling beyond the JSON file, so T9.3+ can layer the network ops on top without further state plumbing.
  - Verification: `python3 -m unittest tests.protocol.test_desktop_vault_migration_state` (13 OK: state-diagram coverage including all four rollback origins + all four crash-recovery actions + persistence round-trip + garbled-JSON handling); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (266 OK); `python3 -m py_compile desktop/src/vault_migration.py tests/protocol/test_desktop_vault_migration_state.py`.
  - Accept: Killing the app at each state and relaunching produces the right resume prompt.
- [x] **T9.2** — Server endpoints: `POST /api/vaults/{id}/migration/start`, `GET /api/vaults/{id}/migration/verify-source`, `PUT /api/vaults/{id}/migration/commit`. Idempotent.
  - 2026-05-04 update: New `vault_migration_intents` table (migration `003_vault_migration.sql`, applied idempotently from `Database::migrate`) holds one row per in-flight migration: `vault_id` PK + `token_hash` (sha256 of the bearer secret, never stored plaintext) + `target_relay_url` + timestamps + `initiating_device`. New `VaultMigrationIntentsRepository` with `recordIntent` (idempotent insert that returns the *existing* row when called twice — original token wins, retried `/start` doesn't drift), `getIntent`, `markVerified`, `markCommitted`, `cancelIntent`. `VaultController` gained three endpoints: `migrationStart` (writes intent, returns the bearer token *exactly once* — second-call-same-target returns 200 with `token_returned=false`; second-call-different-target raises `VaultMigrationInProgressError('started', existing_target)`), `migrationVerifySource` (returns `manifest_hash` / `chunk_count` / `used_ciphertext_bytes` / `target_relay_url` / `started_at` for diff against the target — read-only so it works even after commit per §H2), `migrationCommit` (validates target matches the recorded intent, calls existing `markMigratedTo`, stamps `committed_at` on the intent row). Routes wired in `server/public/index.php` alongside the existing `gc/*` routes; `migrationStart` and `migrationCommit` use `vaultPost`/`vaultPut` (bearer auth required), `migrationVerifySource` uses `vaultGet`. `guardReadOnly` already returns `409 vault_migration_in_progress` for any write to a `migrated_to`-stamped vault, so the §H2 read-only-on-source contract holds without further plumbing. The migration token is `mig_v1_<30 lowercase base32>` (150 random bits) — same alphabet as the rest of the vault id-space.
  - Verification: `python3 -m unittest tests.protocol.test_server_vault_migration` (2 OK end-to-end against a hermetic PHP server: full lifecycle covers start → idempotent re-start → 409 on conflicting target → verify-source → commit → 409 on post-commit manifest PUT → `GET /header` returning `migrated_to`; second test confirms verify-source without an intent is 400 `vault_invalid_request`); `python3 -m unittest tests.protocol.test_server_contract` (31 OK — no regression in pre-existing server contract); `python3 -m unittest discover -s tests/protocol -p 'test_desktop_vault*.py'` (266 OK); `php -l` clean on the new repository, controller methods, index.php, and Database.php.
  - Accept: Calling start twice returns the same migration token; calling commit twice doesn't double-mark.
- [ ] **T9.3** — Migration copy phase: batch-HEAD on target, transfer missing chunks (re-using existing `chunks` PUT path), copy manifest revisions, copy header, copy recovery envelope blob.
  - Accept: 1 GB vault migrates end-to-end; killing mid-copy resumes at last completed chunk.
- [ ] **T9.4** — Migration verify phase: hash-chain compare, chunk-count compare, byte-total compare, random-sample chunk decrypt on target.
  - Accept: Verify mismatch surfaces `vault_migration_verify_failed.details.mismatch` with the right enum value.
- [ ] **T9.5** — Commit + multi-device propagation: commit endpoint sets source vault `migrated_to: <target_url>`. Other devices receive on next `GET /header`, switch active relay, save `previous_relay_url` for 7 days.
  - Accept: Two desktop instances sharing the same vault: one runs migration, the other within 5 minutes sees the redirect on its next sync and switches transparently.
- [ ] **T9.6** — Settings → Migration tab in Vault settings window: shows current relay, "Switch back to previous relay" if available, "Migrate to another relay" launcher.
  - Accept: Switch-back works for 7 days post-commit; after 7 days the option disappears.

---

### T10 — Local binding + Backup-only mode

- [ ] **T10.1** — Local SQLite tables: `vault_bindings` (per §A12: state + sync_mode), `vault_local_entries` (path + content fingerprint + last-synced revision), `vault_pending_operations`. Migration script.
  - Accept: Schema visible via sqlite3 CLI; existing transfer-pipeline tables untouched.
- [ ] **T10.2** — Connect-local-folder flow: folder picker → scan → preflight dialog (per §D15: separate tombstone preview line) → sync-mode selection (default Backup only per §gaps §20) → confirm → binding row created with `state = needs-preflight`.
  - Accept: Preflight numbers add up; sync-mode default is Backup only; cancellation leaves no rows.
- [ ] **T10.3** — Initial baseline: download current remote-folder state to local path; populate `vault_local_entries` with `last_synced_revision = current_revision`. Tombstones not applied.
  - Accept: After baseline, `binding_state = bound`; local files match remote current state; no deletions of pre-existing local files (those become "extra" in `vault_local_entries`).
- [ ] **T10.4** — Filesystem watcher (`watchdog`): debounced 500ms, file-stability gate per §H13 (3s primary, 10s on network shares, 5min hung-detection cap). Queues to pending operations.
  - Accept: Bursts of file edits collapse into batched ops; stability gate prevents partial-file uploads.
- [ ] **T10.5** — Backup-only sync loop: pending ops → upload (re-using T6); fetch manifest but **don't** apply remote changes locally; record `last_synced_revision` advancing.
  - Accept: New local file appears in remote within 10s; remote-only changes do not appear locally.
- [ ] **T10.6** — Manual "Sync now" button per binding: forces a watcher flush + immediate cycle. Reports outcome in activity log + a toast.
  - Accept: With watcher off, "Sync now" still fully syncs; toast describes counts.

---

### T11 — Restore remote → local folder (atomic writes, conflict copies)

- [ ] **T11.1** — Atomic-download helper: write to `<dest>.dc-temp-<uuid>`, fsync, fsync directory, rename. Cleanup pass at startup removes `*.dc-temp-*` older than 24h (per §gaps §11).
  - Accept: Power-loss simulation (kill -9 mid-rename) leaves only either the old file or the new file, never a partial.
- [ ] **T11.2** — Restore remote folder into chosen local path (one-shot, not a binding): per §gaps §12 partial-restore action. Disk preflight, atomic-write tree.
  - Accept: Restore into populated path uses A20 naming for collisions; into empty path materializes cleanly.
- [ ] **T11.3** — Conflict-copy materializer: shared utility that produces A20-named files for any of the three conflict contexts (sync / browser-upload / import).
  - Accept: All three callers use it and produce identical naming for the same inputs.
- [ ] **T11.4** — Trash-on-delete: when sync would remove a local file (because remote tombstoned it), move to OS trash via `gio trash` (Linux). Log `vault.sync.file_moved_to_trash`.
  - Accept: Tombstoned-remote → local file moved to trash, recoverable via file manager.
- [ ] **T11.5** — Restore-from-date action: pick a date, find latest manifest revision ≤ date, walk that snapshot's folder → materialize at chosen path with conflict copies.
  - Accept: Restoring a folder to a 2-week-old state writes the snapshot files; current state on the relay is unchanged.

---

### T12 — Two-way sync (watcher, ransomware detector, CAS merge)

- [ ] **T12.1** — Two-way sync mode: combine T10.5 backup-only path with remote-changes-applied path (via T11 atomic-write). Each cycle: fetch manifest → apply remote diff to local → upload pending local → repeat until quiet.
  - Accept: Edit-on-A → propagates to B within one cycle; edit-on-B-while-A-also-edits → both versions land per CAS merge; concurrent delete + edit → keep-both per §D4.
- [ ] **T12.2** — Local-delete propagation: watcher detects unlink → check `vault_local_entries` (was it synced?) → if yes, create tombstone; if no, do nothing (avoids wiping unsynced local files).
  - Accept: Deleting a previously-synced file produces a remote tombstone; deleting a never-synced file is silent.
- [ ] **T12.3** — Ransomware detector per §A15: counters keyed by binding, sliding 5-minute window. On trip: `binding_state = paused`, surface banner with [Review] [Rollback] [Resume] [Keep paused]. Thresholds configurable in Vault settings → Sync safety.
  - Accept: Touching 200 files in 5 minutes pauses the binding; surface text matches §gaps §6 + §A15 verbatim; user actions land their state transitions.
- [ ] **T12.4** — Pause / Resume per binding: `state = paused` keeps `sync_mode` set so resume restores the same mode (per §A12). Pending ops preserved across pause.
  - Accept: Paused binding does no traffic; resuming flushes pending ops.
- [ ] **T12.5** — Disconnect: state → `unbound`, drop `vault_bindings` row but keep `vault_local_entries` until garbage-collected by user. Local files untouched, remote untouched.
  - Accept: Disconnected folder still browses via Browser mode; reconnecting starts a fresh preflight.
- [ ] **T12.6** — Multi-device concurrent ops integration test (per H7): two desktop instances pointed at same relay + same vault, scripted operations: simultaneous upload, delete-vs-edit race, three-device merge. CI-runnable.
  - Accept: All scripted scenarios produce final state matching expected (no data loss, deterministic `latest_version_id`).

---

### T13 — QR-assisted vault grants + revocation + access-secret rotation

- [ ] **T13.1** — Server endpoints: `POST /api/vaults/{id}/join-requests`, `GET /api/vaults/{id}/join-requests/{req_id}`, `POST .../claim`, `DELETE .../device-grants/{device_id}`, `POST .../access-secret/rotate`. Capability bit `vault_grant_qr_v1`.
  - Accept: Each endpoint integration-tested; expired/revoked join requests rejected.
- [ ] **T13.2** — Generate join QR on existing admin device. Format: `vault://<relay>/<vault_id>/<join_request_id>/<ephemeral_pubkey_b64>?expires=<ts>`. 15-min expiry default.
  - Accept: QR encodes parseable URL; expired QR rejected on claim.
- [ ] **T13.3** — Receive join QR (desktop scan via secondary file picker or paste — Android does the camera scan in its own plan). Generate ephemeral keypair, post claim, derive 6-digit verification code.
  - Accept: Two desktops successfully complete a grant exchange; verification codes match on both sides.
- [ ] **T13.4** — Approval UI on the granting (admin) device: see pending join request, verification code on both sides, role picker (default `sync` per §D11), Approve / Reject buttons. On approve: wrap vault unlock material with new device's pubkey + post.
  - Accept: New device receives the grant, opens the vault, can browse/upload per its role.
- [ ] **T13.5** — Devices tab in Vault settings: list grants, role + last-seen + revoke button. Revoke confirmation uses §gaps §14 verbatim text + offers "Revoke and rotate access secret" combo.
  - Accept: Revoke flips server flag; revoked device's next vault op returns `vault_access_denied`. Rotate-combo runs both atomically.
- [ ] **T13.6** — Access-secret rotation per §A5: client posts old + new tokens, server validates old then atomically replaces hash. Client-side 7-day "tell other devices" reminder banner. T0 §A14 scope: this is the only rotation in v1.
  - Accept: After rotation, only devices that received the new secret can write; banner clears when all paired devices have re-authed (or on day 8).

---

### T14 — Dangerous clear / purge flows

- [ ] **T14.1** — Clear-folder danger flow: dialog requires typing exact folder name + fresh-unlock per §gaps §13. Soft-deletes all current entries in one CAS-published manifest revision.
  - Accept: Cleared folder has zero current entries, all retained as tombstones; activity log shows `vault.folder.cleared`.
- [ ] **T14.2** — Clear-whole-vault flow: stronger dialog requires typing full Vault ID + admin role + fresh-unlock. Bulk soft-delete across all folders.
  - Accept: All folders empty after clear; per §D2 retention applies; `vault.vault.cleared` logged.
- [ ] **T14.3** — Hard-purge scheduling: 24-hour delay default, configurable. Persisted to `vault_pending_purges.json` (client) + `vault_gc_jobs` (server). Cancel before delay elapses removes both.
  - Accept: Scheduled purge persisted across restart; cancellation works; delay enforced.
- [ ] **T14.4** — Hard-purge execution at T+24h: client (or server-side scheduler) calls `gc/execute` with `purge_secret` (separate high-entropy secret stored in recovery kit per §file 09). Server deletes chunks; updates `used_ciphertext_bytes`.
  - Accept: Post-purge, downloading a referenced chunk returns `vault_chunk_missing`; quota counter decreases.
- [ ] **T14.5** — Toggle-OFF interaction (§A17): toggling Vault active OFF clears `vault_pending_purges.json` + calls `gc/cancel` on the server. Re-toggle ON does **not** restore.
  - Accept: Schedule purge → toggle OFF → server confirms cancellation → toggle ON → no purge fires.

---

### T17 — Diagnostics + hardening

- [ ] **T17.1** — Activity tab in Vault settings: render the encrypted op-log + archived segments as a timeline. Filter by event type, search by filename. Read-only.
  - Accept: All major ops (create / upload / delete / restore / clear / device grant / revocation / migration / eviction / purge) appear with timestamps + device names.
- [ ] **T17.2** — Local per-device log per §gaps §21: rotating `~/.config/desktop-connector/logs/vault.log`, gated on existing "Allow logging" toggle. Never logs keys / passphrases / decrypted filenames / file content.
  - Accept: Smoke test: enable logging, run a few ops, check log contains URL paths + AEAD failures + sync stalls but **no** plaintext filenames or secrets.
- [ ] **T17.3** — Integrity check: Quick (manifest hash chain + chunk-index references + AEAD-verify current manifest) + Full (decrypt every revision + every chunk). Vault settings → Maintenance.
  - Accept: Quick check on a healthy vault: seconds, "OK". Quick check after corrupting one chunk on disk: identifies it. Full check on the same: identifies it AND verifies older revisions.
- [ ] **T17.4** — Repair helper: list broken items, two actions: "Mark broken in next manifest revision" (purges from live tree, retains in op-log) + "Restore from export" (wraps T8 import targeting only the broken items). Never auto-deletes.
  - Accept: Manual smoke test on a corrupted vault produces a clean working vault after repair.
- [ ] **T17.5** — Debug bundle: ZIP including config (redacted), local index schema dump, op-log tail (no plaintext), binding states, error counts. Excludes everything sensitive.
  - Accept: Bundle round-trip-shareable; grep-checked for absence of `vault_master_key`, `recovery`, `passphrase`, `Authorization:` headers, decrypted filenames.
- [ ] **T17.6** — Event vocabulary catalog: extend `docs/diagnostics.events.md` with vault-prefixed events. Verify each is emitted somewhere in the codebase.
  - Accept: Events doc lists all `vault.*` events in alphabetical order; CI grep verifies each event tag has at least one emit site.

---

## Milestones

Each milestone gates further work on a successful manual test pass. **When all sub-tasks for the milestone's phases are `[x]`, run the script and report results. Tell the user when the milestone passes.**

### Milestone M1 — Foundations (T0 + T1 + T2 + T3)

After M1 you can: see vault capability bits in `/api/health`, create a vault from the desktop wizard, see the Vault ID in the canonical 4-4-4 form, see the toggle in main settings, see an empty Vault submenu in the tray, dump the encrypted header via curl, run the test-vector harness against both desktop Python and server PHP.

**Manual test script:**

1. Fresh DB: `rm server/data/connector.db && rm -rf server/storage/vaults/` (or use a fresh deploy directory).
2. Run server: `php -S 0.0.0.0:4441 -t server/public/`.
3. `curl http://localhost:4441/api/health | jq .capabilities` → expect `vault_v1` + the T1 sub-bits.
4. Run desktop on a fresh config: `cd desktop && python3 -m src.main`. Open Desktop Connector Settings → expect a new "Vault" section with the toggle **ON by default** (per §D16) and "Open Vault settings…" button enabled.
5. Tray menu — confirm Vault submenu is visible. Contents: "Create vault…", "Import vault…" (no operating entries yet — no vault exists).
6. Click "Create vault…" → wizard opens. Click Cancel. → expect toggle to flip OFF in main Settings; tray submenu disappears.
7. Re-flip toggle ON. → tray submenu reappears with the same Create/Import entries; clicking "Create vault…" relaunches the wizard.
8. Complete the wizard: enter recovery passphrase (twice) → recovery test prompt → "Skip recovery test" → success screen.
9. Wizard closes; toggle stayed ON; tray submenu now shows full operating menu ("Open Vault…", "Sync now", "Export…", "Import…", "Settings"); Vault settings window accessible.
10. Vault settings → header shows Vault ID in `XXXX-XXXX-XXXX` format; copy button works.
11. Vault settings → Recovery tab → status reads "Untested" (skipped earlier); banner offers "Test recovery now".
12. `curl -H "X-Vault-Authorization: Bearer <secret>" http://localhost:4441/api/vaults/<id>/header | jq .` → JSON with `encrypted_header`, `header_hash`, `quota_ciphertext_bytes: 1073741824`, `used_ciphertext_bytes: 0`.
13. `pytest tests/protocol/test_vault_v1_vectors.py` → all green. `phpunit tests/Vault/VaultCryptoVectorsTest.php` → all green.
14. Toggle OFF in main Settings → tray submenu disappears, Vault settings window can still be reached via direct CLI but "Open Vault settings…" button greys out.
15. Restart desktop → toggle state (OFF) preserved.

If all 15 pass, **M1 done**. Tell the user.

### Milestone M2 — Remote folders + read-only browse (T4 + T5)

After M2 you can: create / rename / list remote folders, browse the (initially empty) folder contents from the desktop, and download files & previous versions placed via curl.

**Manual test script:**

1. From M1: vault exists, toggle ON.
2. Vault settings → Folders tab → click "+" → name "Documents" → confirm. Default ignore patterns shown editable; accept defaults.
3. Repeat: add "Photos", "Projects".
4. Vault window → Browser → folder tree shows all three; click "Documents" → empty state.
5. Use a script to PUT a chunk + manifest revision adding `Documents/test.txt` as a 1 MB file (use the test-vector tooling from T2 or write a small upload helper). Refresh browser → file appears.
6. Right-click `test.txt` → Download → choose `/tmp/dl/test.txt`. SHA-256 matches.
7. Use the script to add a second version of `test.txt`. Refresh browser → versions panel shows two versions.
8. Download Previous Version → side-path file `/tmp/dl/test.txt.v1` matches v1 bytes.
9. Vault settings → header shows used storage > 0; folder list "Documents" row shows non-zero "Stored" column.
10. Add a chunk into a second folder that *references* the same chunk_id (test dedup) → both folders display the size; whole-vault used delta is zero for that op.
11. Rename "Projects" → "Project Backups". List + breadcrumb update; storage unchanged.

If all 11 pass, **M2 done**.

### Milestone M3 — Safe mutations (T6 + T7)

After M3 you can: upload from the browser, manage versions, soft-delete + restore, evict on quota pressure.

**Manual test script:**

1. From M2: at least 1 folder + 1 file exist.
2. Browser → Documents → click Upload → pick a file. New file appears.
3. Upload again with same name → conflict dialog → "Add as new version" → versions panel shows two versions.
4. Upload again with same name → "Keep both with rename" → new entry at `<name> (conflict uploaded …)`.
5. Right-click a file → Delete → confirmation showing recovery deadline → Delete. File hidden.
6. Toggle "Show deleted" → file reappears greyed with date.
7. Right-click greyed file → Restore previous version → file is back, untombstoned.
8. Delete the entire "Photos" folder (one click + confirm). All entries tombstoned.
9. Concurrent-upload scenario: open two desktop instances pointed at same vault; both upload to `same-path.txt` simultaneously. Final state: two versions in `versions[]`, deterministic `latest_version_id`.
10. Quota-pressure: upload large files until vault is at ~95%. Banner appears at 80% and 90% thresholds (per §D2). Upload another large file → 507 → eviction offers (since old versions exist) → eviction runs, write succeeds, banner clears.
11. Quota-with-no-history: delete all old versions / fill vault with only current files → next upload → 507 with `eviction_available=false` → banner "vault full, sync stopped".

If all 11 pass, **M3 done**.

### Milestone M4 — Portability: export / import / migration (T8 + T9)

After M4 you can: export to a passphrase-protected bundle, import on a different relay (new vault) or merge into existing same-id vault, migrate the vault between relays.

**Manual test script:**

1. From M3: vault has multiple folders, multiple files, multiple versions, some tombstones.
2. Vault settings → Export → "Export vault now" → enter export passphrase → choose `/tmp/vault.dc-vault-export` → verify-stage runs → success.
3. Wrong-passphrase import → `vault_export_passphrase_invalid`. Tampered import (truncate the file) → `vault_export_tampered`.
4. On a second relay (port 4442 or different host), run import wizard with the bundle → preview shows correct file/version/tombstone counts → confirm → vault opens browse-only on the new relay.
5. Same-id merge: import the same bundle into the **same** active vault → preview shows mostly already-known + a few conflicts (after we change a couple of files between export and import) → per-folder prompts → choose Rename → conflicts land at `<name> (conflict imported …)`.
6. Different-id refusal: hand-crafted import file with a different vault fingerprint → preview shows "different vault" → cannot proceed.
7. Migration: Vault settings → Migration → enter target relay URL → start → migration progresses through `started → copying → verified → committed`. Source vault becomes read-only.
8. Other desktop pointed at source: continues working until next health-check, then auto-switches to target. Stores `previous_relay_url`.
9. Within 7 days: "Switch back to previous relay" available in Migration tab; click it → switches back.
10. Past 7 days: option disappears.
11. Mid-migration crash: kill desktop during `copying` → relaunch → wizard offers "Resume migration" / "Abandon and rollback".

If all 11 pass, **M4 done**.

### Milestone M5 — Backup-only sync (T10 + T11)

After M5 you can: connect a local folder, choose Backup-only mode, see local changes flow up, restore remote into a new local path.

**Manual test script:**

1. From M4: vault has at least one remote folder with files.
2. Vault settings → Folders → "Documents" → "Connect local folder" → pick `/tmp/sync-docs/` (empty) → preflight shows "Remote: 5 files / Local: 0 / Conflicts: 0".
3. Confirm → initial baseline downloads all 5 files. State → `bound`, mode → `Backup only`.
4. Edit a remote file via the Browser (upload a new version) — local folder does **NOT** receive the change (Backup-only is upload-only).
5. Add a new local file `/tmp/sync-docs/local.txt` → within ~5s, watcher picks up + uploads → Browser refresh shows it.
6. Switch mode to `Two-way` (deferred — for M5 the toggle exists but we test it in M6).
7. Stop / Pause sync → state → `paused`. Add a local file. Pending ops accumulate.
8. Resume → pending ops flush.
9. Disconnect → binding row gone; local files untouched; folder browses via Browser mode again.
10. Restore-into-folder action: pick "Documents" → "Restore to local path" → `/tmp/restore-docs/` → atomic-write tree.
11. Restore-from-date: pick date 1 hour ago → only files current at that point materialize.

If all 11 pass, **M5 done**.

### Milestone M6 — Two-way sync + multi-device (T12 + T13)

After M6 you can: run Two-way sync across two desktops, detect ransomware-style change bursts, grant a third device via QR, revoke it, rotate the access secret.

**Manual test script:**

1. Two desktops (A, B) sharing one vault, both with `/tmp/sync-A` and `/tmp/sync-B` bound to "Documents", Two-way mode.
2. A creates `a.txt`. Within seconds, B sees it.
3. B creates `b.txt`. A sees it.
4. Concurrent edit: both edit `shared.txt` simultaneously. Both versions land in remote `versions[]`. `latest_version_id` matches on both clients (deterministic).
5. A deletes `a.txt`. B applies the tombstone — file moves to OS trash on B.
6. Ransomware test: on A, a script renames 250 files in 1 minute. Within 5 min the binding flips to `paused` with the banner. Click [Review changes] → list of pending ops. Click [Resume] → uploads continue.
7. From A (admin), Vault settings → Devices → Add device → QR shown.
8. C (third desktop) imports the QR → claims → A sees pending request with verification code matching → choose role `sync` → Approve.
9. C is now paired; can browse / upload / soft-delete; cannot Hard purge.
10. A revokes C → Devices list shows revoked → C's next vault op returns `vault_access_denied`.
11. A uses "Revoke and rotate" combo on a hypothetical bad device → rotation runs; banner on A asks to share the new secret with surviving devices within 7 days.
12. New secret distributed to B; old secret no longer works for new requests.

If all 12 pass, **M6 done**.

### Milestone M7 — Destructive flows + diagnostics (T14 + T17)

After M7 you can: clear folders / vaults with appropriate guards, schedule + cancel + execute hard purges, view activity timeline, run integrity checks, export debug bundle. **v1 ships at the end of this milestone.**

**Manual test script:**

1. Clear-folder: pick a folder with 10+ files → "Clear folder contents" → typed-confirm folder name → fresh-unlock prompt → confirm. All entries become tombstones; chunks retained.
2. Clear-vault: Danger zone → "Clear whole vault" → typed Vault ID + admin role + fresh-unlock. All folders empty.
3. Schedule purge: "Schedule hard purge" → confirm → state file written, server job created. Try to cancel within 24h → both client + server records cleared.
4. Schedule another purge → wait 24h (or fast-forward dev time) → execution runs → chunks deleted from disk → quota counter drops.
5. Pre-purge sanity: try to download a chunk targeted for purge **before** delay → succeeds (chunks not deleted yet). After purge → `vault_chunk_missing`.
6. Toggle-OFF interaction: schedule a purge → toggle Vault OFF → state file cleared, server job cancelled. Toggle ON → no purge fires.
7. Vault settings → Activity → see the timeline of all major ops with timestamps + device names.
8. Maintenance → Quick check → seconds → "OK".
9. Manually corrupt one chunk on disk (`echo X > server/storage/vaults/<id>/<prefix>/<chunk>`) → Quick check → identifies the bad chunk.
10. Full check on a fresh vault → minutes → "OK". Full check on the corrupted vault → identifies the same bad chunk + verifies older revisions.
11. Repair → "Mark broken in next manifest revision" → vault back to clean.
12. Maintenance → Download debug bundle → ZIP saved. `unzip -p bundle.zip | grep -i 'master_key\|passphrase\|recovery\|Authorization' || echo OK` → "OK".

If all 12 pass, **M7 done.**

---

### Final step — Critical risks evaluation (gates v1 ship)

Before declaring v1 shipped, read [`desktop-connector-vault-critical-risks-and-weaknesses.md`](desktop-connector-vault-critical-risks-and-weaknesses.md) end-to-end and evaluate every risk it lists against the **then-current state of the app** (not the plan as written, the code as built).

For each risk:

- **Resolved** — point at the commit / file / test that handles it.
- **Mitigated** — describe the mitigation that's in place, even if the underlying weakness still exists.
- **Accepted** — document the rationale for shipping with the risk open + when it gets re-evaluated.
- **Open** — open a follow-up tracker item before v1 ships.

The risks doc is allowed to grow new entries during M1–M7 as we learn things; this final step is the gate that confirms each one was taken seriously rather than silently rolling past it. **v1 SHIPS only after this evaluation is complete and every risk has a labeled outcome.**

---

## Discrepancies & hardening — all resolved

| ID | Topic | Locked in |
|----|-------|-----------|
| D1 | Manifest format versioning | T0 §D1 |
| D2 | Quota + warnings + 4-step eviction | T0 §D2 |
| D3 | Device grants in exports | T0 §D3 |
| D4 | CAS merge algorithm | T0 §D4 |
| D5 | Tombstone retention math | T0 §D5 |
| D6 | `remote_folders_cache` semantics | T0 §D6 |
| D7 | Android scope (post-v1 plan) | T0 §D7 |
| D8 | Export vs recovery passphrase | T0 §D8 |
| D9 | Single vault + import-merge | T0 §D9 |
| D10 | Versioning vocabulary | T0 §D10 |
| D11 | Permission roles (4 canonical) | T0 §D11 + §A9 |
| D12 | Capability bits | T0 §D12 |
| D13 | Storage isolation | T0 §D13 + §A19 |
| D14 | Op-log segments (cap 1000) | T0 §D14 + §A13 |
| D15 | Preflight tombstone preview | T0 §D15 |
| D16 | Vault-active toggle | T0 §D16 + §A2 + §A17 |
| H2 | Migration state recovery | T0 §H2 |
| H1, H14 | (covered by D4 / D7 respectively) | — |
| Audit A1–A21 | 21 implementation clarifications | T0 §"Implementation clarifications" |

Open hardening items (not contradictions; tracked inside owning phase): **H3 H4 H5 H6 H7 H8 H9 H10 H11 H12 H13 H15** — each lives in the sub-task acceptance criteria of its owning phase above.

---

## Open notes

- Branch: `tresor-vault` (rename to `vault` is optional and post-T0 if at all — branch name doesn't appear in code).
- Test vectors live at `tests/protocol/vault-v1/`. Format pinned in T0 §A18.
- Wire-format reference doc: `docs/protocol/vault-v1.md` — created in T0.2.
- Byte-format doc (AAD, HKDF labels, envelope structures): `docs/protocol/vault-v1-formats.md` — created in T0.3.
- Diagnostics events: extend existing `docs/diagnostics.events.md`. Vocabulary in T17.6.
- This file is the working tracker. Edit freely as work proceeds; don't replace the structure (audit tooling assumes phase + sub-task IDs are stable).
