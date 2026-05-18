# Vault v1 build items — agreed scoping (2026-05-18)

Single index for the six v1 builds locked in during the 2026-05-18 design pass. Each section captures: **decision · rationale · UX flow · code anchors · sizing · sub-questions · tests**. None of these are implemented yet; the design choice is recorded so implementation can pick up without re-litigating scoping.

Sibling plan docs:
- [`vault-eviction-v1.md`](vault-eviction-v1.md) — §3.C1 (eviction algorithm + threat model)

Index of build items below:

1. [`§5.C1` — Migration wizard](#5c1--migration-wizard) (also resolves §5.M2 + §5.M6 as subordinate fixes)
2. [`§5.C2` — QR-join + grant approval UI](#5c2--qr-join--grant-approval-ui)
3. [`§6.H3` — Export wizard](#6h3--export-wizard)
4. [`§6.H2` — Devices tab + revoke-device UI](#6h2--devices-tab--revoke-device-ui)
5. [`§5.H2` — Per-folder import conflict resolution](#5h2--per-folder-import-conflict-resolution)
6. [`§5.H3` — Access-secret rotation](#5h3--access-secret-rotation)

---

## §5.C1 — Migration wizard *(landed 2026-05-18)*

**Status:** landed. Multi-page subprocess `desktop/src/windows_vault_migration.py` (invokable as `python3 -m src.windows vault-migration`); spawned from `windows_vault/tab_migration.py`'s "Migrate to another relay…" button (no longer disabled). Pages: setup (target URL + test connection) → confirm (chunk count + bytes total from new `migration_preflight` helper) → progress (real-time `MigrationProgress` callback on copy + verify) → done (switch-back banner copy) plus an error page for verify mismatches. The engine's `run_migration` drives all transitions; the wizard just marshals callbacks back to the GTK main loop and persists the post-commit `previous_relay_url` + `vault_previous_relay_expires_at` into config so the existing Migration-tab switch-back surface keeps working.

**Bundled fixes:**

- ~~**§5.M6** — `previous_relay_url` stale carry~~ *(landed)*. Added `clear_previous_relay()` helper in `desktop/src/vault/migration/state.py`; the wizard calls it at the start of every fresh start/verify/commit cycle so A → B → C records `previous = B` rather than the stale A. Regression-pinned by `test_a_to_b_to_c_records_previous_b_not_a`.
- **§5.M2** — shard genesis-insert for rev > 1. **Not landed**; surfaced to the operator instead. The new `MigrationInventory.has_edited_shards` flag fires when any folder's `shard_revision > 1`, and the wizard's Confirm page shows an explicit warning that the resume path may hit the idempotency gap. Fresh-vault migrations work end-to-end; edited-vault migrations work on the first attempt but fail on partial-completion resume. Full fix is a separate task — see `unfinished.md` §5.M2.

**Decision** *(2026-05-18)*: build the full multi-page wizard for v1. Inline subordinate fixes for §5.M2 (shard genesis-insert for rev > 1) and §5.M6 (`previous_relay_url` stale carry).

**Rationale.** The engine — state machine, target bootstrap, verification, `on_committed` callback, copy progress — is in place. Production callers are zero. A minimal "advanced URL entry" hatch was rejected because the migration is a destructive cross-relay operation; users need preflight diff + verify + commit safety nets, not a one-field power-user box. Shipping v1 without any UI was rejected because the placeholder button is already in `tab_migration.py` — leaving it disabled past v1 signals "this is a permanent gap."

### UX flow

Five-page subprocess (`windows_vault_migration.py`), brand-styled stepper at top:

1. **Source / target picker.** Source relay = current (read-only). Target relay URL entry + connection test button. The connection test exercises the existing private-host filter (`migrationAllowPrivateUrls`); operators flip the relay-side config if they intentionally point at `127.0.0.1`.
2. **Preflight diff.** Library: `desktop/src/vault/migration/runner.py::preflight_inventory`. Surfaces: chunk count + bytes to copy, manifest revisions per shard, any active GC plans on source. Block "Continue" if source has unresolved 507s or active forced-eviction plans.
3. **Progress / cancel.** Library: `run_migration(on_progress=...)`. Two progress bars: chunks copied, shards verified. Cancel button works at any chunk boundary (engine already has the pause primitives).
4. **Verify.** Library: `verify_migration_consistency` (already exported from runner). Re-reads target root + each shard and compares hash chains. Surfaces verification mismatches if any.
5. **Commit + switch-back controls.** Commit calls `commitMigration` server endpoint on the *source* relay (which marks the source's manifest sealed-pointing-at-target). Adds the "switch back to source" button visible for 24h post-commit (uses the existing `previous_relay_url` field, which §5.M6 fixes).

After commit: the desktop's config-dir `relay_url` flips to target; banner across all subprocesses reads "Migrated to <new relay> · Switch back" for 24h, then disappears.

### Code anchors

**Ready to use (no changes):**
- `desktop/src/vault/migration/runner.py` — `run_migration`, `preflight_inventory`, `verify_migration_consistency`, `commitMigration`. State machine + target bootstrap done.
- `desktop/src/vault/migration/state.py` — `_record_dict_with_previous_relay` (will gain a `clear_previous_relay()` call from §5.M6 inline fix).
- Server-side `migrationStart` / `migrationCommit` + the private-host filter (landed `ff499e6`).
- `desktop/src/windows_vault/tab_migration.py` — the placeholder button + button copy already shipped.

**To build:**
- `desktop/src/windows_vault_migration.py` — new subprocess module. ~600 LOC GTK + threading + AT-SPI labels. Mirror the import wizard's shape (`windows_vault_import.py`) for module structure.
- `desktop/src/windows_vault/tab_migration.py:54-63` — wire the placeholder button to spawn the new subprocess via `python3 -m src.windows vault-migration --config-dir=...`. Drop the `set_sensitive(False)` call.
- `desktop/src/runners/migration_runner.py` (new) — bridges the wizard subprocess to the engine; mirrors `pairing_runner.py` shape.

**Subordinate fixes bundled in this build:**
- **§5.M2 shard genesis-insert.** `desktop/src/vault/migration/runner.py:476-503` — the `_bootstrap_target_and_inventory` re-entry path. Fix: walk the shard chain from rev=1 with synthesized envelopes when the idempotent re-entry sees `current_hash != shard_hash`. One-commit fix; lands alongside the wizard.
- **§5.M6 `previous_relay_url` stale carry.** `desktop/src/vault/migration/state.py:172-174` — add `state.clear_previous_relay()` call at the start of every fresh start/verify/commit cycle so A → B → C records `previous=B` not `previous=A`.

### Sizing

3–5 days. Single follow-up PR. The 600 LOC subprocess is mechanical (mirrors `windows_vault_import.py`); the threading work is mostly bridging to engine callbacks. Risk concentrates in the verify page's copy + the post-commit switch-back banner UX (both need careful copy testing).

### Sub-questions

1. **Concurrent migration on multiple paired devices.** Two desktops both opening the wizard at the same time race. Server should reject the second migrationStart with 409. *Recommendation: pin behavior in a test; engine already handles 409 by treating it as "another device is migrating; show their progress" — wizard just needs to surface that.*
2. **Switch-back banner visibility scope.** 24h is the engine's grace window. *Recommendation: pin at 24h; document in the ADR.*
3. **Multi-folder verify ordering.** Does verify walk shards alphabetically or by `remote_folder_id`? *Recommendation: by `remote_folder_id` (stable across runs).*

### Tests to add

- `test_migration_wizard_preflight_blocks_on_unresolved_507`
- `test_migration_wizard_cancel_at_chunk_boundary_resumable`
- `test_migration_wizard_verify_surfaces_hash_mismatch`
- `test_migration_wizard_commit_flips_relay_url`
- `test_migration_wizard_switch_back_button_clears_after_24h`
- `test_migration_runner_shard_genesis_for_rev_gt_1` (§5.M2 regression-pin)
- `test_migration_state_a_to_b_to_c_records_previous_b_not_a` (§5.M6 regression-pin)

---

## §5.C2 — QR-join + grant approval UI *(landed 2026-05-18)*

**Status:** landed. Admin in-process modal: `desktop/src/windows_vault/grant_device_dialog.py` (opened from the Devices tab's "Grant a new device…" button). Claimant subprocess: `desktop/src/windows_vault_join.py` (invokable as `vault-join`, surfaced via tray's new "Add this device to a vault…" entry when no local vault exists). Typed client + raw HTTP methods: `desktop/src/vault/grant/join_client.py` + new methods on `VaultHttpRelay`. Paste-URL flow only; webcam QR scanning is the documented v1.x follow-up. Tests: `tests/protocol/test_desktop_vault_join_{client,flow_source}.py`. Diagnostics: 11 new `vault.grant.*` events.

**Decision** *(2026-05-18)*: build the QR-assisted device-grant UI for v1. **Reverses the prior memory note** `project_vault_multi_device_story.md` which classified this as v1.x.

**Rationale.** The wire protocol is shipped end-to-end (`vault/grant/qr.py`, `vault/grant/wrap.py`, server `/join-requests` + `/device-grants`, capability `vault_grant_qr_v1`). With Devices tab also being built (§6.H2), the multi-device story becomes "grant from existing device + revoke if lost" — coherent. Recovery-kit-per-device stays available as the secondary path. Shipping v1 with only the kit path leaves the protocol primitives sitting unused.

### UX flow

Three new surfaces:

1. **Claimant view** (joining device): "Add this device to a vault" entry from Vault home when no vault is unlocked locally. Two input modes:
   - Scan QR from another device's screen (uses existing `qrcode` lib for rendering; for scanning, integrate `python-zbar` or `pyzbar` — see sub-questions).
   - Paste join URL (`vault://join?...`).
   After parsing, claimant generates a fresh wrap pubkey + verification code, posts a join-request, surfaces "Show this code to your existing device: **`<6-digit-code>`**", and polls for the device-grant approval.
2. **Admin approval dialog** (existing device): pops up on the admin's desktop when a join-request lands. Displays: claimant device name (from join-request body), requested role, the 6-digit verification code (read aloud / typed). Two buttons: "Approve" (wraps the grant + posts to `/device-grants`), "Reject" (deletes the join-request).
3. **Verification flow**: admin reads the code aloud; claimant types/confirms. Code mismatch → both sides see "Code did not match. Cancel and retry." Forces out-of-band channel + defeats relay MITM.

### Code anchors

**Ready to use:**
- `desktop/src/vault/grant/qr.py::make_join_url, parse_join_url`
- `desktop/src/vault/grant/wrap.py::wrap_grant_for_claimant, unwrap_grant_for_claimant`
- Server: `/api/vaults/{id}/join-requests/{req_id}/{claim, approve}`, `/device-grants`, capability advertisement.

**To build:**
- `desktop/src/windows_vault_join.py` (new subprocess) — claimant view. Scan/paste mode toggle, verification-code display, polling state machine.
- `desktop/src/runners/join_runner.py` — bridges the join subprocess to the relay client + wrap library.
- `desktop/src/windows_vault/admin_approval_dialog.py` — admin-side approval modal, spawned from the existing settings subprocess when a join-request observation fires.
- Polling for join-requests: extend the existing `VaultGrantPoller` (or create one) so the admin's tray + Settings subprocess both observe `GET /join-requests/pending`.

### Sizing

3–4 days. The crypto wiring is one-day work (libraries are ready). The two-subprocess coordination (claimant + admin) is the costly part — separate processes, separate polling loops, atomic state transitions.

### Sub-questions

1. **QR scanning library on Linux.** `pyzbar` (pure Python ctypes wrapper around `libzbar0`) is the cleanest. `pyzbar` requires `apt install libzbar0`; document in `bootstrap/dependency_check.py` + `install.sh`. *Recommendation: pyzbar; degrade to paste-only mode if `libzbar0` missing.*
2. **Where does scanning happen — webcam or screen-grab?** Webcam (GTK4 `GstWidget` + GStreamer pipeline). Screen-grab adds Wayland portal complexity. *Recommendation: webcam first; document the Wayland-portal screen-grab path as a v1.x enhancement.*
3. **Verification code length / format.** 6 digits per spec §3.3; pinned. Confirmation, not open question.
4. **Concurrent claimants.** F-S13 already covers (CAS-on-pending; 200/409 split). Pin behaviour in a wizard-level test that races two claimants.
5. **Admin rejection — does the join-request expire or auto-delete?** Auto-delete via the existing `/join-requests/{req_id}` DELETE endpoint. Reject button calls DELETE then dismisses dialog.

### Tests to add

- `test_join_wizard_paste_url_parses_into_join_request`
- `test_join_wizard_verification_code_mismatch_blocks_grant`
- `test_admin_approval_dialog_wraps_grant_correctly`
- `test_admin_rejection_deletes_join_request`
- `test_two_concurrent_claimants_one_grant_succeeds_one_409s` (build on existing §7.L1 test)
- `test_join_url_with_wrong_vault_id_rejected_at_parse_time`

### Memory update required

`project_vault_multi_device_story.md` "How to apply" rules now wrong. Update to: "QR-grant is the primary v1 device-add path; recovery-kit stays as secondary recovery surface."

---

## §6.H3 — Export wizard *(landed 2026-05-18)*

**Status:** landed. Subprocess `desktop/src/windows_vault_export.py` (`vault-export`) walks setup → progress → done with optional shred. The setup page validates passphrase length + confirm match before enabling Continue (the data-layer `EXPORT_PASSPHRASE_MIN_LEN=8` is the floor; the wizard nudges towards ≥16 via the inline strength hint). Verify-after-write defaults on — the worker calls `read_export_bundle` against the just-written file before the success screen, catching the rare bit-flip class of failure. Shred uses the existing `shred_file` helper behind an explicit confirmation dialog.

**Wired entry points:** tray submenu's "Export…" entry restored (was pulled when only a notification stub existed); Vault Settings → Recovery tab gains an "Export vault…" button alongside the existing recovery actions. Dispatcher entry `vault-export` registered in `windows.py`.

**Diagnostics:** `vault.export.{started, completed, verified, shredded, failed}` cataloged. Tests: `tests/protocol/test_desktop_vault_export_wizard_source.py`.

**Decision** *(2026-05-18)*: build the GTK export wizard for v1. Reuses the recovery-kit code path's UX shape.

**Rationale.** `write_export_bundle` is shipped with zero non-test callers. The CLI helper alternative loses fresh-unlock + Argon2id-off-main-thread progress UX. Recovery-kit-only was rejected because export-to-file is a distinct user task (full vault snapshot for migration / backup-to-USB) that the kit doesn't satisfy.

### UX flow

Single subprocess (`windows_vault_export.py`):

1. **Path picker** — `Gtk.FileChooserDialog` (Save dialog) — default name `vault-export-<YYYY-MM-DD>.dcvault`. User picks destination.
2. **Passphrase entry** — two fields (passphrase + confirm), strength meter, "Use a strong passphrase" guidance copy. Library: existing `vault_passphrase.py` strength scoring.
3. **Progress** — Argon2id derivation off main thread (mirror import wizard's worker pattern). Two-bar progress: derivation %, then bundle write %.
4. **"Shred bundle after copy" toggle** — secondary action that runs the bundle through `shred -uvz` after a successful copy elsewhere (user confirms they've moved it to their target). Optional; default off.
5. **Success screen** — bundle path, SHA-256, "Verify bundle" button. "Verify" re-reads the bundle and walks its envelope chain, surfacing any read-time corruption.

### Code anchors

**Ready to use:**
- `desktop/src/vault/export/bundle.py::write_export_bundle` — the data-layer entry point.
- `desktop/src/vault_passphrase.py` — strength meter + Argon2id derivation.
- `desktop/src/windows_vault_import.py` — sibling wizard to mirror for module shape.

**To build:**
- `desktop/src/windows_vault_export.py` — new subprocess module (~400 LOC, smaller than import because no merge UX).
- `desktop/src/runners/export_runner.py` — bridge between subprocess and `write_export_bundle`.
- Tray menu entry restoration: re-add the "Export…" submenu item in `desktop/src/tray/vault_submenu.py`, pointing at the new subprocess (removed entry was `_vault_export_stub`).

### Sizing

1–2 days. Smaller scope than import (no conflict resolution, no merge math). Risk concentrates in the "Verify bundle" path needing careful surfacing of corruption modes.

### Sub-questions

1. **Verify on success — opt-in or default?** *Recommendation: default-on. Catches `write_export_bundle` writing a corrupt bundle (rare but bug-class).*
2. **Shred toggle semantics — confirmation step needed?** Yes — clicking "Shred" should pop a "This permanently deletes the bundle. Have you confirmed it's at the target?" confirmation. Use the existing destructive-action dialog pattern.
3. **Bundle compression — gzip the output?** Reusing existing `write_export_bundle` behaviour (no compression; uses AEAD overhead). *Recommendation: no compression in v1; the ciphertext is already incompressible.*

### Tests to add

- `test_export_wizard_passphrase_strength_blocks_weak_input`
- `test_export_wizard_verify_succeeds_on_freshly_written_bundle`
- `test_export_wizard_verify_surfaces_tampered_bundle`
- `test_export_wizard_shred_action_unlinks_bundle`
- `test_export_wizard_cancel_during_argon2_safe` (no partial bundle written)

---

## §6.H2 — Devices tab + revoke-device UI *(landed 2026-05-18)*

**Status:** landed. Code anchors: `desktop/src/vault/grant/client.py`, `desktop/src/windows_vault/tab_devices.py`, `desktop/src/vault/binding/runtime.py` (`list_device_grants` + `revoke_device_grant` methods on `VaultHttpRelay`), `desktop/src/windows_vault/main_window.py` (placeholder loop trimmed). Tests: `tests/protocol/test_desktop_vault_devices_{client,tab_source}.py`. Diagnostic: `vault.device.revoked`.

**Decision** *(2026-05-18)*: build the full Devices tab (list grants + revoke) for v1.

**Rationale.** Server endpoints (`revokeDeviceGrant`, `listGrants`) shipped + tested; the desktop side is entirely missing. This is v1's largest UX gap — a vault that can grant device access but cannot revoke it has no defense against a lost paired desktop. With §5.C2's QR-grant build, revoke becomes the necessary partner ("grant from existing device, revoke if device lost"). CLI/curl path was rejected because lost-laptop scenario is exactly when users panic + need in-app revoke.

### UX flow

Replaces the placeholder Devices tab in `windows_vault/main_window.py:188-207`.

Single page, card-per-row layout. Each row:
- Device name (defaulted from grant body's `device_name`, editable inline).
- Role (admin / sync).
- Last seen timestamp (`last_seen_at` from server).
- "Revoke" button (icon + label; brand-orange for destructive).
- Revoked rows greyed out, sort to bottom; "revoked at" stamp.

**Revoke flow** (per spec §3.3):
1. Click "Revoke" → confirmation dialog with locked copy: **"Revoking this device prevents future Vault access. It cannot erase data already copied to that device."**
2. Confirm gates: fresh-unlock + admin-role (mirror `tab_danger.py` double-gate).
3. Type vault name to confirm (existing pattern from `confirm_vault_clear_text_matches`).
4. Submit → desktop calls `/api/vaults/{id}/devices/{device_id}/revoke` with `purge_secret` from the unlock.
5. On success: row flips to revoked state; toast confirmation. Lock the confirmation copy with a source-pin test (per spec §3.3) so future copy edits don't regress.

### Code anchors

**Ready to use:**
- Server: `VaultGrantsController::revokeDeviceGrant`, `listGrants` (paginated).
- Server: capability `vault_grant_qr_v1` already advertises revoke availability.
- `desktop/src/windows_vault/tab_danger.py` — the fresh-unlock + admin-role + type-to-confirm pattern to mirror.

**To build:**
- `desktop/src/vault/grant/client.py` (new) — HTTP adapter: `list_device_grants(vault_id)`, `revoke_device_grant(vault_id, device_id, purge_secret)`. Typed responses (dataclasses); 401/403 → `VaultAuthError`; 404 → `DeviceGrantNotFoundError`. Retry on transient 5xx via the existing relay-error shape.
- `desktop/src/windows_vault/tab_devices.py` — replace the placeholder. Reactive list refresh after every grant/revoke. Mirror Folders tab shape for card layout.
- `desktop/src/windows_vault/main_window.py:188-207` — wire the new tab module in place of the placeholder, drop the "reserved for later development" copy.

### Sizing

2–3 days. Heaviest v1 build in pure LOC (~500 LOC GTK + HTTP + tests + brand styling), but design pattern matches Folders + Danger tabs already in production.

### Sub-questions

1. **Listing scope — show revoked grants forever, or hide after N days?** *Recommendation: keep forever in v1 (audit trail). v1.x can add a "Hide old revoked" toggle.*
2. **Self-revoke — can the current desktop revoke itself?** No — server should 409 with `cannot_revoke_self`. Desktop UI greys out the Revoke button on the current device's row + tooltip "Use 'Disconnect this device' instead."
3. **Last admin protection — can the last admin be revoked?** No — server 409s with `last_admin_lockout`. Desktop surfaces the error inline.
4. **Reactive refresh — polling or push?** *Recommendation: polling on tab open + every 30 s while visible. Push (via fasttrack message?) is a v1.x optimization.*
5. **Device-rename in this tab — same write surface as the grant's `device_name`?** Yes — `PATCH /api/vaults/{id}/devices/{device_id}` with the new name. Existing pattern from grant edits.

### Tests to add

- `test_devices_tab_renders_grants_with_revoke_button`
- `test_revoke_dialog_locked_copy_matches_spec_3_3_verbatim` (source-pin)
- `test_revoke_self_blocked_with_inline_error`
- `test_revoke_last_admin_blocked_with_inline_error`
- `test_revoke_success_marks_row_revoked_and_refreshes`
- `test_revoke_requires_fresh_unlock_and_admin_role`
- `test_devices_tab_polls_on_visibility_change`

---

## §5.H2 — Per-folder import conflict resolution

**Decision** *(2026-05-18)*: build the per-folder conflict page for v1. Closes the spec §17 gap; library `find_conflict_batches` already exists.

**Rationale.** Conservative rename-only default leaves users without overwrite/skip choices — surprising for a "wizard" that supposed to be transparent. Global picker on Preview page is too coarse (a single import often mixes folders where some should rename, some skip). Per-folder page satisfies spec verbatim.

### UX flow

New page inserted between **Preview** and **Progress** in `windows_vault_import.py`. Only shown if `find_conflict_batches` returns non-empty for the import.

Page contents:
- Header: "These folders have name conflicts. Choose how to resolve each."
- One card per conflicting folder. Each card:
  - Folder name (left) — bundle's folder.
  - Current vault content (right column, summarized: "12 files, 4 conflicting names").
  - Three radio buttons: **Rename** (default, conservative) / **Overwrite** / **Skip**.
  - "Apply to remaining folders with the same conflict kind" button — fills in the same choice for all subsequent same-kind folders below.
- Footer: "Continue" disabled until every folder has a choice.

### Code anchors

**Ready to use:**
- `desktop/src/vault/import_/conflicts.py::find_conflict_batches` — library function that returns `[(remote_folder_id, conflict_kind, count), ...]`.
- `desktop/src/windows_vault_import.py` — existing wizard module to extend.

**To build:**
- New page class inside `windows_vault_import.py` (or a new sibling module `windows_vault_import_conflicts.py` if file size gets unwieldy). ~300 LOC GTK + AT-SPI labels.
- `ImportMergeResolution(per_folder={...})` wiring: today the wizard builds this with `per_folder={}` (line 36, 364). Replace with the page's output: `per_folder={folder_id: ImportFolderMode.RENAME, ...}`.

### Sizing

1 day. Self-contained page addition; no library or server work.

### Sub-questions

1. **"Apply to remaining same-kind" — what defines "same kind"?** *Recommendation: tuple of (conflict reason, folder direction). Currently `find_conflict_batches` returns enums per-folder; pin the equivalence in a test.*
2. **Skip semantics — skip the whole folder or just the conflicting files?** Whole folder per spec §17. Pin in a test.
3. **Resolution surfacing in Progress page** — should the Progress page show "Skipped: 3 folders" / "Overwritten: 2"? *Recommendation: yes; surface counts in the success screen too.*

### Tests to add

- `test_conflict_page_renders_card_per_conflicting_folder`
- `test_apply_to_remaining_fills_same_kind_folders_only`
- `test_continue_disabled_until_all_folders_chosen`
- `test_skip_whole_folder_excludes_all_its_files_from_merge`
- `test_overwrite_per_folder_resolves_only_that_folder`

---

## §5.H3 — Access-secret rotation *(landed 2026-05-18)*

**Status:** landed. Server endpoint was already shipped at T13.6 (`POST /api/vaults/{id}/access-secret/rotate` admin-gated + idempotent + audit-logged). v1 build adds the client surface:

- **Subprocess** `desktop/src/windows_vault_rotate.py` (`vault-rotate`) walks confirm → verify-existing-kit → progress → save-new-kit. Two safety checkboxes gate Continue. Kit pick + passphrase re-verify happens BEFORE the rotation POST so the new kit can carry the same passphrase-derived material. After rotation the local keyring grant is swapped atomically (`VaultGrant.from_bytes(vault_id, master_key, new_secret)`) so the next vault op uses the new bearer. The save-kit page blocks Close until the operator writes the new kit; force-close surfaces an "are you sure?" confirmation explicitly mentioning that recovery is permanently lost.
- **Typed client** `desktop/src/vault/grant/rotate_client.py` wraps the new `VaultHttpRelay.rotate_access_secret` raw method into `RotationResponse` + `RotationAuthError` / `RotationRateLimitedError` / `RotationNotFoundError`.
- **Tab wiring** — `windows_vault/tab_recovery.py`'s "Update recovery material" button is no longer force-disabled; clicking it spawns the wizard.
- **Diagnostics** — `vault.rotate.{started, server_committed, kit_saved, kit_save_failed}` cataloged.

Tests: `tests/protocol/test_desktop_vault_rotate_{client,wizard_source}.py`. Closes the latent-bomb gap where eventual rotation would silently invalidate every kit on the relay side.

**Decision** *(2026-05-18)*: scope a v1 build that bundles (a) "Rotate access secret" button, (b) confirmation dialog, (c) post-rotation kit regeneration, (d) server `/rotate` endpoint + auth hooks. Tooltip drops the "not implemented" copy.

**Rationale.** Library is ready; nothing breaks today only because nothing calls it. When rotation eventually happens, existing kits become silently undecryptable on the relay side (right master_key, wrong bearer) — that's a latent bomb. Building rotation forces the kit-regeneration step into the same flow, eliminating the bomb.

### UX flow

Single subprocess (`windows_vault_rotate.py`):

1. **Confirmation page.** Heading: "Rotate vault access secret." Body explains: "This invalidates all existing recovery kits and device grants. You'll need to download a fresh kit after rotation." Two checkboxes that must be ticked: "I understand existing recovery kits stop working", "I'll save the new kit before closing this window". Continue button disabled until both ticked.
2. **Fresh-unlock + admin-role gate.** Mirror `tab_danger.py`.
3. **Progress page.** Generate new access secret (library: `access_rotation.py::generate_new_secret`). Submit `/rotate` request body (library: `rotation_request_body`). Show "Rotating…" spinner while server processes.
4. **Save kit page.** New recovery kit auto-generated + path picker. Same dialog pattern as initial kit creation. **Cannot close this window without confirming "I've saved the kit."**
5. **Success screen.** "Rotation complete. Save the kit somewhere safe." Reminder: existing devices need to re-paste their grant or get re-granted via QR.

### Code anchors

**Ready to use:**
- `desktop/src/vault/grant/access_rotation.py::generate_new_secret, rotation_request_body, reminders`.
- `desktop/src/windows_vault/tab_recovery.py` — recovery tab placeholder; tooltip "Recovery-material rotation is not implemented yet" is dropped.

**To build:**
- Server: new `POST /api/vaults/{id}/rotate` endpoint in `VaultController.php`. Body: `rotation_request_body` shape. Auth: admin-role + fresh `purge_secret` style gate. Atomically updates the vault's access secret; existing grants stop being valid.
- `desktop/src/windows_vault_rotate.py` — new subprocess module.
- `desktop/src/vault/grant/rotate_client.py` (new HTTP adapter) — calls `/rotate` with retry on transient 5xx.
- `desktop/src/windows_vault/tab_recovery.py` — wire the "Rotate access secret" button, drop the disabled tooltip.

### Sizing

2–3 days. Server endpoint is the riskiest part — needs careful transactional handling around the access-secret swap (the swap and the grant invalidation must be atomic or replay-safe).

### Sub-questions

1. **Existing device grants after rotation — invalidated or auto-re-issued?** Invalidated. Re-issue requires re-running the QR-grant flow (§5.C2). Pin in test.
2. **Last-rotation timestamp — surface in Settings → Recovery?** Yes — "Last rotated: <date>" line. Use the existing `reminders.py` library which already computes rotation reminders.
3. **Rate-limit on `/rotate`** — server-side cap of 1 rotation per 24h? *Recommendation: yes; prevents accidental double-rotation lockout.*
4. **What if user closes the kit-save page without saving?** The window pre-confirmation prevents this (cannot close until ticked). If the process crashes mid-save, the rotation is committed but the kit is lost. *Recommendation: surface a recovery banner on next launch — "Rotation completed but kit was not saved. Generate a new kit now to keep recovery available."*

### Tests to add

- `test_rotation_confirmation_blocked_until_both_checkboxes`
- `test_rotation_requires_fresh_unlock_and_admin_role`
- `test_rotation_invalidates_existing_device_grants`
- `test_rotation_atomically_updates_access_secret`
- `test_rotation_cannot_close_kit_save_page_until_saved`
- `test_rotation_rate_limit_blocks_second_rotation_within_24h`

---

## Cross-cutting notes

- **Brand styling** for all wizards: blue-dominant per `docs/visual-identity-guide.md`. Use existing `vault_brand_dialog.py` helpers for destructive actions (revoke, rotate).
- **AT-SPI labels** for every new dialog (`docs/testing/vault-tests.md` lists the convention). Every new button needs a unique stable a11y name so the chained-test harness can drive it.
- **Event vocabulary** for `docs/diagnostics.events.md`:
  - `vault.migration.{started, preflight_ok, progress, cancelled, verified, committed}`
  - `vault.join_request.{claimed, code_mismatch, approved, rejected}`
  - `vault.device_grant.{listed, revoked, revoke_blocked_self, revoke_blocked_last_admin}`
  - `vault.import.conflict_resolution.{per_folder_chosen, applied_to_remaining}`
  - `vault.export.{started, progress, completed, verified, shredded}`
  - `vault.rotate.{started, server_committed, kit_saved, kit_save_failed}`
- **ADR entries to add** alongside each implementation commit (per `docs/architecture-decisions.md` convention).
- **Memory updates**: `project_vault_multi_device_story.md` needs amendment to reflect §5.C2 reversal — done as part of this 2026-05-18 design pass.

## Suggested implementation order

If picked up serially:

1. **§6.H3 Export wizard** (1–2 days) — smallest, restores the removed tray entry, builds the Argon2id-off-main-thread + bundle-write pattern for later reuse.
2. **§5.H2 Per-folder import** (1 day) — extends the existing import wizard; quick win.
3. **§5.H3 Rotation** (2–3 days) — server endpoint + UI; unblocks the kit-regeneration flow that §5.C2 depends on later.
4. **§6.H2 Devices tab** (2–3 days) — needed before §5.C2 so revoke is available the day grants ship.
5. **§5.C2 QR-join + grant approval** (3–4 days) — depends on §6.H2 being able to revoke if granted in error.
6. **§5.C1 Migration wizard** (3–5 days) — largest; bundles §5.M2 + §5.M6 fixes.

Total: ~12–18 days serial. Some pairs (§5.H2 + §6.H3, §6.H2 + §5.H3) parallelize cleanly.
