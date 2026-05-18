# Vault v1 — outstanding follow-ups

Single source of truth for every item from the max-effort review (`temp/finished-plans/max-review-result.md`) that did NOT land as a code fix. Detail-per-entry inlined here so you don't have to cross-reference the archive.

Three categories:

1. **Needs-design** (§1) — fix scope requires explicit user decision before any code lands. Each has 2–3 resolution paths.
2. **Deferred Low** (§2) — reviewer-classified as polish-tier, acceptable for v1, or operator-deployment caveats.
3. **Summary count** (§3) — verified by grepping the archived tracker.

Last reconciled against `temp/finished-plans/max-review-result.md` on 2026-05-17.

---

## 1. Needs-design (await user scoping)

12 entries — 2 Criticals + 3 Highs + 4 Mediums + 3 partials (with primary fix landed but follow-up gap tracked here).

### §3.C1 — Eviction stages 2/3 hard-purge: `purge_secret` UI *(partial)*

**Status:** partial-needs-followup
**Verified against:** `server/src/Controllers/VaultController.php:1111-1284`, `desktop/src/vault/ops/eviction.py:130-275`, `desktop/src/windows_vault_browser/quota.py:30-150`

**Doubt:** Spec §D9 ("destructive-action ledger") requires hard-purges to need **admin + purge_secret + fresh_unlock**. Eviction stages 2/3 now require admin role (commit f621dc1) but still do NOT verify `purge_secret` or surface a passphrase prompt. The 507-quota dialog in `QuotaMixin._handle_quota_exceeded` is a one-click "Reclaim space" confirmation; closing the gap requires a passphrase entry on that dialog so the desktop can derive `purge_secret` and pass it to `/api/vaults/{id}/gc/execute`. That UI change is meaningful enough (brand-styled dialog + Argon2id-off-main-thread interaction with §6.C1/§6.C2 fixes + retry path on wrong passphrase) that it deserves explicit user scoping before landing.

**Action taken:** admin-role gate landed via new `KIND_FORCED_EVICTION` plan kind and `purpose='forced_eviction'` body param on `gc/plan`. Server enforces admin on both plan creation and execution. Desktop threads `purpose` from stages 2/3.

**Need from user:** confirmation on whether to (a) add passphrase prompt to the 507 dialog (closes spec gap, breaks today's one-click UX), or (b) document acceptance in ADR + spec amendment leaving stages 2/3 at admin-only.

---

### §5.C1 — Migration wizard UI

**Status:** skipped-needs-design (new feature build)
**Verified against:** `desktop/src/windows_vault/tab_migration.py:54-63` (button is `set_sensitive(False)`, comment "the engine is ready"); `desktop/src/vault/migration/runner.py` exports `run_migration` but `grep -RE "run_migration\b" desktop/src/ | grep -v test` returns only the runner's own definition + self-references — zero production callers.

**Doubt:** The library (state machine, target bootstrap, verification, on_committed callback, copy progress) is in place. What's missing is a multi-page GTK wizard: source-relay target picker → preflight diff → progress/cancel page → verify confirmation → commit + switch-back controls. That's a dedicated `windows_vault_migration.py` subprocess with at least ~600 lines of UI + threading + AT-SPI labels and a brand-styled stepper. Building UI surface autonomously violates the per-issue protocol's "never build a new feature autonomously" rule.

**Action taken:** nothing; logged for explicit scoping.

**Need from user:** decision on (a) build the wizard as a single follow-up PR (sized ~3–5 days), (b) ship v1 without migration UI and document the engine as "library-only, v1.1 wizard", or (c) inline a minimal "advanced URL entry" admin escape hatch that calls `run_migration` directly without the wizard polish.

---

### §5.C2 — QR-join + grant approval UI

**Status:** skipped-needs-design (new feature build)
**Verified against:** `desktop/src/vault/grant/qr.py:make_join_url`, `desktop/src/vault/grant/wrap.py:wrap_grant_for_claimant / unwrap_grant_for_claimant`, `parse_join_url` — all exported, zero non-test callers in `desktop/src/`. Memory note `project_vault_multi_device_story.md` already records that v1 multi-device is recovery-kit-only.

**Doubt:** Building this requires three new UI surfaces — claimant "scan/paste join URL" view, admin "approve a join request" dialog, orchestrator hitting `/join-requests/{req_id}/{claim,approve}`. Plus a way for the claimant to surface a verification code that the admin must read aloud. That's a multi-day feature build that the explicit memory note says is **v1.x future work, not a v1 gap**.

**Action taken:** nothing; the recovery-kit + import-wizard path is the shipping multi-device story.

**Need from user:** confirmation that QR-join can stay v1.1 (so this Critical drops to "won't fix for v1.0, tracked elsewhere"), or scoping for the build.

---

### §5.H2 — Per-folder import conflict resolution UI

**Status:** skipped-needs-design (new feature build)
**Verified against:** `desktop/src/windows_vault_import.py:36, 364` (`ImportMergeResolution(per_folder={})` always — module docstring admits "Conflict-resolution UI is not yet wired here"); `desktop/src/vault/import_/conflicts.find_conflict_batches` is the library function with zero non-test callers in `desktop/src/`.

**Doubt:** The wizard currently defaults to `rename` (the conservative, data-loss-free option) for every conflicting folder, so the failure mode is "user can't pick overwrite or skip per folder" rather than "data corruption". Spec §17 calls for per-folder conflict batches with an "Apply to remaining" button — that's a new wizard page between Preview and Progress with N controls + a "Apply to all remaining folders with the same conflict kind" affordance. Building that page autonomously violates the per-issue protocol's "never build a new feature autonomously" rule (~300 LOC of GTK + threading + AT-SPI labels + brand styling).

**Action taken:** nothing; the conservative default (rename) keeps the shipping behaviour safe. The library is ready.

**Need from user:** decision on (a) build the per-folder conflict page as a follow-up PR, (b) ship v1 with the rename-only default and document the gap as "v1.1 enhancement", or (c) inline a single global picker on the Preview page (rename / overwrite / skip for the whole import) as a minimal step before the per-folder UI.

---

### §5.H3 — Access-secret rotation has no client trigger

**Status:** skipped-needs-design (new feature build)
**Verified against:** `desktop/src/vault/grant/access_rotation.py:65-110` (`generate_new_secret`, `rotation_request_body`, reminders all exported; zero non-test callers in `desktop/src/`); `desktop/src/windows_vault/tab_recovery.py:56` tooltip reads "Recovery-material rotation is not implemented yet".

**Doubt:** Until rotation is wired, nothing breaks — the library waits for callers. This is a pre-emptive risk: when rotation lands, every existing recovery kit becomes silently undecryptable on the relay side (right master_key, wrong bearer), so the wizard has to prompt for kit regeneration in the same flow. Building the trigger requires (a) a "Rotate access secret" button under Settings → Recovery, (b) a confirmation dialog explaining "this invalidates your existing recovery kits", (c) the post-rotation recovery-kit regeneration step, (d) a server-side `/rotate` endpoint + auth hooks. (d) is also missing today.

**Action taken:** nothing; current shipping behaviour is "no rotation" which is safe pending the build.

**Need from user:** decision on whether v1 ships without rotation (documented as v1.x), or scope a build that bundles UI + server endpoint + kit-regeneration prompt together.

---

### §6.H1 — Scheduled-purge auto-executor needs `purge_secret` persistence *(partial)*

**Status:** partial-needs-followup
**Verified against:** `desktop/src/vault/ops/purge_schedule.py:171-186` (`build_execute_request_body` requires `purge_secret: str`); `vaults.purge_token_hash` BLOB column in `server/migrations/002_vault.sql:32` (server-optional, never set by the desktop's create flow).

**Doubt:** To wire the autosync to literally call `gc/execute` on a due purge, the desktop needs `purge_secret` in scope at the moment the autosync fires. There are three paths and each has real trade-offs:

  (a) Generate `purge_token_hash` at vault-create time, record `purge_secret` in the recovery kit, persist a keyring copy at `schedule_purge` time, read it from the keyring during autosync. Full automation — but the keyring stores a long-lived purge-fire credential, which is a new at-rest secret class. Need user buy-in.

  (b) Push the schedule to the relay as a real `KIND_SCHEDULED_PURGE` row with a server-side cron — server fires when due. Removes the desktop's "must be online" constraint entirely. But the relay currently has no scheduler infra; would need a small cron + retention policy.

  (c) Leave fire-on-attended (current behaviour with the partial fix): autosync notifies, user reopens Vault Settings → Danger zone, completes with the recovery kit. Dialog copy is now honest about this.

**Action taken:** partial fix landed (commit 0b836aa) — autosync notifies on due purges, dialog copy clarifies the online dependency. `vault.purge.due_awaiting_user` event documented. The "auto-fire" half is still open.

**Need from user:** pick (a), (b), or (c). (c) ships as-is.

---

### §6.H2 — Revoke-device UI (entire Devices tab)

**Status:** skipped-needs-design (new feature build)
**Verified against:** `desktop/src/windows_vault/main_window.py:188-207` (four tabs — devices, security, sync_safety, storage — are literal "This panel is reserved for later development" placeholders); `server/src/Controllers/VaultGrantsController.php:420` ships `revokeDeviceGrant` + `listGrants` endpoints; `grep -r "Revoking this device" desktop/` returns empty.

**Doubt:** The server endpoints (revoke + list active/revoked grants) are shipped and tested. The desktop side has zero library wrappers calling them, and the Devices tab is a placeholder. Building a real Revoke UI requires:
  - A `list_device_grants` / `revoke_device_grant` client helper (HTTP adapter + typed responses + retry/auth glue).
  - GTK page listing active grants in a card-per-row layout with a per-row Revoke button, "last seen", device_name attribution.
  - Locked confirmation copy verbatim per §3.3: "Revoking this device prevents future Vault access. It cannot erase data already copied to that device." A locked-string source-pin test so future copy edits don't regress the wording.
  - Fresh-unlock + admin-role double-gate (existing pattern from `tab_danger.py`).
  - Reactive refresh of the row list after a successful revoke.

A v1 vault that can grant device access but cannot revoke it has no defence against a lost paired desktop — this is the heaviest v1 gap of the §6 batch. Building the surface autonomously violates the per-issue protocol's "never build a new feature autonomously" rule (~500 LOC of GTK + HTTP + tests + brand styling).

**Action taken:** nothing; the spec gap remains open.

**Need from user:** decision on (a) build the Devices tab as a follow-up PR (sized ~2–3 days; mirrors the Folders tab's shape with the destructive-action gate pattern from `tab_danger`), or (b) ship v1 with revoke as a CLI helper / direct curl against the server endpoint and document the desktop-UI gap as a v1.x target.

---

### §6.H3 — Export wizard *(partial — Sync-now landed, Export removed pending wizard)*

**Status:** removed-needs-design (new feature build)
**Verified against:** `desktop/src/vault/ui/ui_state.py:101` (tokens list no longer includes `"export"`); `desktop/src/tray/vault_submenu.py` (the `_vault_export_stub` method is gone, the menu item is gone); `desktop/src/vault/export/bundle.py:write_export_bundle` is the shipped data-layer entry point with zero non-test callers in `desktop/src/`.

**Doubt:** Wiring "Sync now" to actually fire the autosync was a one-liner — the in-process loop already existed. Wiring "Export…" would require a brand-new GTK subprocess (`windows_vault_export.py`): path picker, passphrase entry + confirm + strength meter, Argon2id-off-main-thread progress with the §6.C* worker pattern, optional "shred bundle after copying" toggle, success screen with "Verify bundle" action. That's the kind of feature build the per-issue protocol says not to do autonomously.

**Action taken:** tray entry removed (commit b3d84ad) so it no longer fires theatre notifications. The Sync-now half landed as a real fix.

**Need from user:** decision on (a) build the export wizard as a follow-up PR (sized ~1–2 days; mirrors the import wizard's shape), (b) ship v1 with the recovery-kit path as the only recovery surface (memory `project_vault_multi_device_story` already says this is the v1 multi-device story), or (c) inline a minimal "Export to file…" CLI helper as a power-user escape hatch.

---

### §4.M1 — Orphan-chunk stage-0 reaper

**Status:** skipped-needs-design (requires server-side support)
**Verified against:** `desktop/src/vault/upload/folder.py:436-519` (the `_publish_batch_with_cas_retry` exhaust path raises after CAS retries, leaving any chunks already uploaded as orphans); `desktop/src/vault/ops/eviction.py` (stage 1 only purges chunks behind expired tombstones, not active orphans); `server/src/Controllers/VaultController.php` (no endpoint exists that lists all chunk_ids for a vault, so the desktop can't compute the orphan set without enumerating the manifest's chunk references against a server-side inventory).

**Doubt:** The review's suggested fix — a "stage-0 orphan-chunk reaper that does `batch-head × manifest references` and deletes the difference" — requires the desktop to know every chunk currently stored against the vault. The only existing endpoint is `batch_head_chunks` which takes a list and returns presence; it doesn't enumerate. Building the reaper requires either:

  (a) A new server endpoint `GET /api/vaults/{id}/chunks` that lists every stored chunk_id (paginated). Desktop then computes `set(server_chunks) − set(manifest_chunk_refs)` and DELETEs the diff. Simple but adds a new API surface that needs auth + pagination + rate-limit thought.

  (b) Move the reaper to the server. The GC job kind already has `KIND_RECLAIM_AGED_TOMBSTONES` (or similar); a new `KIND_RECLAIM_ORPHAN_CHUNKS` would walk the chunks table, cross-check against the manifest references, and delete. The desktop just triggers the job. Cleaner separation but needs a new state machine + admin gate.

Neither path is a Medium-scope fix. The active-orphan leak is real but bounded: each CAS-exhaust event leaks at most one batch's worth of chunks (typically <100), and the existing 30-day chunk retention policy (server-side) will eventually reap them. The leak's blast radius is "wasted ciphertext quota until retention fires".

**Action taken:** nothing in code. Document the leak shape here so the next eviction-pipeline session has a concrete starting point.

**Need from user:** decision on (a) ship the new `GET /chunks` + desktop reaper, (b) push the work to a server-side `KIND_RECLAIM_ORPHAN_CHUNKS` GC job, or (c) accept the leak as "bounded by retention" and document it in the architecture doc as a known v1 limitation.

---

### §5.M2 — Migration runner shard genesis-insert for rev > 1

**Status:** conditional on §5.C1 (migration wizard)
**Verified against:** `desktop/src/vault/migration/runner.py:476-503` (`_bootstrap_target_and_inventory` has the issue documented in its own comment); §5.C1 logged as needs-design (no production caller hits the runner today).

**Doubt:** Server's `putShard` rejects `new_revision != expected + 1`; the migration runner's idempotent re-entry path requires `current_hash == shard_hash` (which fails on "rejected at validation"). Real-world impact only when migration is wired into the wizard (§5.C1) — until then every callsite is test code that arranges its own genesis state.

**Conditional fix:** bundle this into the §5.C1 migration wizard build. The shard-rev>1 bootstrap path either (a) walks the shard chain from rev=1 with synthesized envelopes, or (b) the server gets a new "accept genesis at arbitrary rev" path gated on the migration intent's verified state. Neither is doable as a standalone Medium.

**Action taken:** nothing in code. Pinned the runner comment block referencing this entry so the migration-wizard session has a clear TODO.

---

### §5.M3 — Cross-subprocess fresh-unlock composition

**Status:** skipped-needs-design (security/UX tradeoff)
**Verified against:** `desktop/src/vault/fresh_unlock.py:38-46` (`_last_unlock_at` is module state, not persisted; docstring explicitly notes the per-process scope); architecture doc §13 promises a unified "single confirm within 15 min idle" window.

**Doubt:** The current design is *more secure* than the spec implies: each subprocess (settings window, import wizard, …) gets its own in-memory state, so a user who typed the passphrase in Settings must retype in the import wizard subprocess. Across-subprocess composition would require persisting the unlock timestamp to disk (config-dir file or local-index SQLite row), which exposes a new attack surface — a malicious local process that can read the config dir can time an attack to fall within the window.

Three resolution paths:

  (a) Persist `_last_unlock_at` to `~/.config/desktop-connector/vault_fresh_unlock.ts`. Every subprocess reads + respects. Implements the spec verbatim but exposes the window to anyone who can read the config dir.

  (b) Use shared memory (POSIX shm + a 5-min TTL). Doesn't touch disk but the shm seg is readable by other processes under the same uid. Same threat model as (a), narrower window.

  (c) Document the per-subprocess scope as an intentional tightening of the spec, and amend the architecture doc to match. Sensitive ops chain only within a single subprocess (e.g. Settings → Danger), and crossing into the import wizard subprocess re-prompts. UX cost: ~1 extra prompt per multi-subprocess session.

**Action taken:** nothing in code. (c) is the lowest-risk shipping path and aligns with the existing implementation.

**Need from user:** confirm (c) as the v1 contract + amend the architecture doc, OR pick (a)/(b) with the threat-model write-up.

---

### §5.M6 — Migration record `previous_relay_url` stale carry

**Status:** conditional on §5.C1 (migration wizard)
**Verified against:** `desktop/src/vault/migration/state.py:172-174` (`_record_dict_with_previous_relay` preserves `previous_relay_url` across migration record overwrites).

**Doubt:** A → B then B → C may carry stale `previous_relay_url=A` in B → C's record if the state file survives. Pre-fix the overwrite-protection is over-cautious — it never replaces a non-None value. Real-world impact only after the migration wizard ships and the user runs a second migration on the same device.

**Conditional fix:** bundle with the §5.C1 migration wizard build. Either (a) explicit `state.clear_previous_relay()` call at the start of a fresh start/verify/commit cycle, or (b) drop the overwrite-protection — A → B → C's intermediate state should mean `previous=B` regardless of what A was. Both are one-line fixes, but the right one depends on the wizard's UX (does the user need to see "previously migrated from A" indefinitely, or only for the rollback window after a commit?).

**Action taken:** nothing in code. The state-machine comment block points at this entry.

---

## 2. Deferred Lows (polish-tier or verified-clean by reviewer)

The reviewer explicitly classified the rest of the Low section as acceptable-for-v1 or operator-deployment caveats. Listed here so they're not lost; not actively tracked as blockers.

### §1 (server) — operator caveats
- **§1.L2** — `migrationCommit` URL parser accepts private IPs / localhost. *Operator-config option; worth optional blocking-by-default with override.* `server/src/Controllers/VaultController.php:1465-1477`
- **§1.L3** — `VaultStorage::ensureDir` chmod 0700 inaccessible to other request pools on shared-host PHP-FPM. *Deployment caveat for shared hosting.* `server/src/VaultStorage.php:45`

### §2 (desktop crypto / sync engine) — polish
- **§2.L2** — CAS retry loop has no overall livelock cap beyond `CAS_MAX_RETRIES`. *Under 10-device write storms a single device could exhaust budget per-folder forever. Log-volume sanity; suggested 50–100 ms × attempt backoff.* `desktop/src/vault/binding/sync.py:1058`
- **§2.L3** — `_imported_rename` caps at 10 000 collisions with `RuntimeError`. *Acceptable sanity bound; flagged for awareness.* `desktop/src/vault/manifest.py:861-866`

### §3 (sync engine internals)
- **§3.L1** — `BatchedUploadStub` orphans reaped only on disconnect, not pause. `desktop/src/vault/binding/lifecycle.py:319-341`
- **§3.L2** — Ignore-dotfiles flag inconsistent across `scan.py` / `preflight.py` / `baseline.py`.
- **§3.L3** — Conflict-path random token is 32 bits; spec-compliant. *Reviewer noted as acceptable.*
- **§3.L4** — `MAX_OP_ATTEMPTS=10` ops sit in the queue forever; no permanent-failure UI surface.

### §6 (desktop UI) — reviewer marked verified-clean
All eight §6 Lows below are pure verified-clean acknowledgements — listed for completeness, not action items:
- **§6.L1** — AT-SPI label includes plaintext filenames. *Acceptable; same sensitivity as visible card title.*
- **§6.L2** — Rollback banner copy correctly mentions fresh-device limitation. *Spec satisfied.*
- **§6.L3** — Activity tab renders destructive events with humanised labels. *Spec satisfied.*
- **§6.L4** — Cross-window state sync is implicit-via-reload, no inotify. *Acceptable for v1.*
- **§6.L5** — No subprocess crash detection; tray doesn't offer to re-open. *UX rough edge.*
- **§6.L6** — `confirm_vault_clear_text_matches` uses `.strip().upper()`. *Matches spec.*
- **§6.L7** — Adw 1.4 fallback gate present via `dependency_check.py`. *Spec satisfied.*
- **§6.L8** — Wizard cancellation correctly preserves toggle. *Memory `feedback_respect_user_intent` satisfied.*

(§6.L9 is a "correction" note that wasn't a real issue — listed in the archive for completeness only.)

### §7 (test polish)
- **§7.L1** — QR claim race lower bound. *No test with two parallel claims with different pubkeys verifying exactly one 200 + one 409.*
- **§7.L2** — Case-insensitive local mount collision (`A.txt` vs `a.txt`) untested. *Linux primary; cross-mount filesystems (NFS, exFAT) still relevant.*
- **§7.L3** — Negative-vector README schema undocumented. *Add `tamper`, `envelope_byte_xor`, `aad_override`, `wrapped_key_byte_xor`, `decrypt_passphrase_override` to `tests/protocol/vault-v1/README.md`.*

**Count: 20 entries unfixed** (2 §1 + 2 §2 + 4 §3 + 8 §6 verified-clean + 1 §6.L9 correction + 3 §7). The **11 actionable polish-tier entries** are §1.L2–L3, §2.L2–L3, §3.L1–L4, §7.L1–L3.

---

## 3. Summary count

Numbers verified by grepping the archived tracker on 2026-05-17.

| Bucket | Total | Fully fixed | Partial fixes | Truly open |
|---|---|---|---|---|
| Criticals | 17 | 14 | 1 (§3.C1) | 2 (§5.C1, §5.C2) |
| Highs | 37 | 32 | 2 (§6.H1, §6.H3) | 3 (§5.H2, §5.H3, §6.H2) |
| Mediums | 35 | 31 | 0 | 4 (§4.M1, §5.M2, §5.M3, §5.M6) |
| Lows | 24 | 4 | 0 | 20 |
| **Total** | **113** | **81** | **3** | **29** |

### Breakdown of the 29 truly-open items

- **12 needs-design** (§1 above): 2 Criticals + 3 Highs + 4 Mediums + 3 partials with follow-up gaps (§3.C1, §6.H1, §6.H3).
- **17 deferred Lows** (§2 above): 2 §1 + 2 §2 + 4 §3 + 8 §6 verified-clean + 1 §6.L9 correction + 3 §7. Of these, the **11 actionable** items are §1.L2–L3 + §2.L2–L3 + §3.L1–L4 + §7.L1–L3.

User-facing math: 3 unfixed Highs + 4 unfixed Mediums + 20 unfixed Lows = 27 not-strikethrough items, plus 2 unfixed Criticals = 29.

---

## 4. Source of truth references

- **Fixes landed:** [`temp/finished-plans/max-review-result.md`](../../temp/finished-plans/max-review-result.md) — every fixed item has a strikethrough heading + commit SHA + Approach paragraph.
- **Append-only fix log:** [`temp/finished-plans/max-review-result-progress.md`](../../temp/finished-plans/max-review-result-progress.md).
- **Historical doubts snapshot:** [`temp/finished-plans/review-doubts.md`](../../temp/finished-plans/review-doubts.md) — the original per-issue doubt log; superseded by §1 of this file. Kept for the commit-time context strings that some code comments reference.
- **This file:** the single live open-item index, kept in `docs/plans/` so it surfaces alongside active planning docs.
