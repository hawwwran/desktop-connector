# Vault v1 — outstanding follow-ups

Single source of truth for every plan item that did NOT land as a code fix. Detail-per-entry inlined here so you don't have to cross-reference the archives.

Three sections:

1. **Max-effort review needs-design** (§1) — fix scope requires explicit user decision before any code lands. Each has 2–3 resolution paths.
2. **Deferred Low** (§2) — review items reviewer-classified as polish-tier, acceptable for v1, or operator-deployment caveats.
3. **Manifest-sharding step 7f cleanup** (§3) — mechanical legacy-API removal still pending from `temp/finished-plans/vault-manifest-sharding.md`. The sharded surface is the production path; the legacy unified-manifest helpers are kept as compat shims while last call sites migrate.
4. **Summary count** (§4) — verified by grepping the archived trackers.

Last reconciled on 2026-05-18.

---

## 1. Decided 2026-05-18 — implementation pending, ordered by priority

All 12 entries from the original "needs-design" bucket have agreed designs as of the 2026-05-18 scoping pass. Six are substantial v1 builds with detailed plans in [`vault-v1-build-items.md`](vault-v1-build-items.md); one is the eviction algorithm in [`vault-eviction-v1.md`](vault-eviction-v1.md) (**landed 2026-05-18** — see entry 1 below); two are documentation-only decisions captured as ADR entries; the remaining two (§5.M2, §5.M6) are subordinate fixes bundled into the §5.C1 migration wizard build.

**Ordering criteria.** Entries below are sorted **by risk + dependency**, not by review section number, severity bucket, or implementation cost:

- **Top of list (priorities 1–2)**: production-risk closer (entry 1, now landed) and the biggest v1 UX gap — ship these first.
- **Middle (3–7)**: missing-capability v1 builds. Internal dependencies pin the order (e.g. §6.H2 Devices tab must land before §5.C2 QR-grant ships, because granting devices without an in-app revoke path is worse than no grants).
- **Lower middle (8)**: housekeeping that's bounded but visible.
- **Tail (9–10)**: resolved-by-ADR decisions with no code work left.
- **Bottom (11–12)**: subordinate fixes that land alongside their parent build (§5.C1).

A separate "smallest-first" implementation order lives in [`vault-v1-build-items.md`'s suggested implementation order](vault-v1-build-items.md#suggested-implementation-order) — that's a different lens on the same set.

---

### 1. ~~§3.C1 — Eviction stages 2/3 hard-purge: `purge_secret` UI~~ *(landed 2026-05-18)*

**Status:** landed. Stages 2 + 3 merged into a single age-ordered destructive iterator in `desktop/src/vault/ops/eviction.py`; the new alarm gate in `desktop/src/windows_vault_browser/quota.py` opens a passphrase prompt when the relay reports `used > quota`. Audit-log signal split: `vault.eviction.auto_purged_oldest` (silent auto-purge to fit an upload) vs `vault.eviction.alarm_purged_oldest` (post-shrink approved cleanup). Stage 1 housekeeping (`vault.eviction.tombstone_purged_expired`) unchanged.

**Plan doc:** [`vault-eviction-v1.md`](vault-eviction-v1.md) — algorithm + threat model + UX comparison + test coverage; commit references in [`architecture-decisions.md`](../architecture-decisions.md) `2026-05-18 — Eviction policy`. Admin-role gate (`KIND_FORCED_EVICTION`, `purpose='forced_eviction'`) stays exactly as it landed in `f621dc1`. The pre-existing `used_bytes` / `used_ciphertext_bytes` key-name mismatch in `VaultQuotaExceededError` was fixed to read both — the alarm gate depends on the values being read correctly.

---

### 2. ~~§6.H2 — Devices tab + revoke-device UI~~ *(landed 2026-05-18)*

**Status:** landed. The placeholder Devices tab in `desktop/src/windows_vault/main_window.py` is replaced by `tab_devices.py` — card-per-row layout listing every grant from `GET /api/vaults/{id}/device-grants`, with Revoke gated behind fresh-unlock → admin-role check (via `relay.get_header().caller_role`) → typed-confirm dialog with the §14 locked copy ("Revoking this device prevents future Vault access. It cannot erase data already copied to that device."). Typed client in `desktop/src/vault/grant/client.py` parses responses into `DeviceGrant` / `RevokeResult` dataclasses and maps server errors (HTTP 400 self-revoke → `CannotRevokeSelfError`; 404 → `DeviceGrantNotFoundError`; 401/403 → `DeviceGrantsAuthError`). Diagnostic event `vault.device.revoked` cataloged. Tab polls every 30 s while visible, clears the timer on unmap. Closes the v1 lost-laptop gap.

Locked-copy + admin-gate + 30 s poll wiring pinned by `tests/protocol/test_desktop_vault_devices_tab_source.py`; client error mapping pinned by `tests/protocol/test_desktop_vault_devices_client.py`.

---

### 3. ~~§5.C2 — QR-join + grant approval UI~~ *(landed 2026-05-18)*

**Status:** landed. Both ends of the QR-grant flow now have GTK surfaces:

- **Admin (in-process modal):** `desktop/src/windows_vault/grant_device_dialog.py` — "Grant a new device…" button at the top of the Devices tab opens a wizard dialog that mints an ephemeral X25519 keypair + POST `createJoinRequest`, renders the join URL as a QR code + plaintext entry, polls `getJoinRequest` every 2 s until `state="claimed"`, shows the 6-digit verification code derived locally from the X25519 shared secret, lets the operator pick a role (`read-only`/`browse-upload`/`sync`/`admin`), wraps a `GrantPayload` via `wrap_grant_for_claimant`, posts `approveJoinRequest`. Cancel triggers best-effort DELETE so abandoned rows don't sit in the per-vault 5-pending budget.
- **Claimant (new subprocess):** `desktop/src/windows_vault_join.py` invokable as `python3 -m src.windows vault-join`, surfaced via the tray submenu's new "Add this device to a vault…" entry (visible when `vault_active=True` and no local vault exists). Paste-URL flow only — webcam scanning deferred to v1.x with `pyzbar` + Wayland portal plumbing. Steps: paste URL → `parse_join_url` + expiry check → fresh X25519 keypair → `claim_join_request` → show verification code + poll for `state="approved"` → AEAD-unwrap via `unwrap_grant_for_claimant` with `expected_vault_id` + `expected_claimant_device_id` pins → persist `VaultGrant` + `config.vault.last_known_id`.
- **Typed client:** `desktop/src/vault/grant/join_client.py` parses raw HTTP into `JoinRequest` dataclass + maps server status codes to `JoinRequestNotFoundError` (404 + `vault_join_request_state`), `JoinRequestStateError` (409 same code), `JoinRequestRateLimitedError` (429 — F-S08 5-pending cap), `JoinRequestAuthError` (401/403). New methods `create_join_request` / `get_join_request` / `claim_join_request` / `approve_join_request` / `reject_join_request` on `VaultHttpRelay`.
- **Diagnostics:** 11 new `vault.grant.*` events cataloged covering both flows' audit anchors + transient/terminal failure modes.

Closes the v1 multi-device gap; QR-grant is now the primary device-add path, recovery kit stays as secondary recovery surface. Pinned by `tests/protocol/test_desktop_vault_join_{client,flow_source}.py`.

---

### 4. §5.C1 — Migration wizard UI *(design landed 2026-05-18)*

**Why this slot:** Critical-bucket missing capability — engine is fully ready, wizard is the only gap. No in-app way to migrate vaults across relays today. Bundles subordinate fixes for §5.M2 + §5.M6.

**Status:** scoped — implementation pending. **Plan:** [`vault-v1-build-items.md#§5.C1`](vault-v1-build-items.md#5c1--migration-wizard).
**Decision:** build the full multi-page wizard for v1; bundles subordinate fixes for §5.M2 + §5.M6. Sized 3–5 days.

---

### 5. §5.H3 — Access-secret rotation client trigger *(design landed 2026-05-18)*

**Why this slot:** preempts a latent bomb. Nothing breaks today *until* rotation happens, at which point all existing recovery kits silently die on the relay side. Building rotation + kit-regeneration prompt together so the bomb never goes off. No urgency today, but ships in v1 to keep the recovery story coherent.

**Status:** scoped — implementation pending. **Plan:** [`vault-v1-build-items.md#§5.H3`](vault-v1-build-items.md#5h3--access-secret-rotation).
**Decision:** build the rotation flow (UI + server endpoint + kit regeneration) for v1. Sized 2–3 days.

---

### 6. §6.H3 — Export wizard *(design landed 2026-05-18)*

**Why this slot:** recovery surface restoration. Recovery kit covers "I lost a device, restore from kit" but not "give me a full vault snapshot to put on a USB stick." `write_export_bundle` is shipped data-layer-ready; only the wizard wraps it. Tray "Export…" entry was removed when it was theatre — this build restores it as a real action.

**Status:** scoped — implementation pending. **Plan:** [`vault-v1-build-items.md#§6.H3`](vault-v1-build-items.md#6h3--export-wizard).
**Decision:** build the GTK export wizard for v1; restore the tray "Export…" entry. Sized 1–2 days.

---

### 7. §5.H2 — Per-folder import conflict resolution UI *(design landed 2026-05-18)*

**Why this slot:** UX polish for the existing import wizard. Today's rename-only default is data-safe — the failure mode is "user can't pick overwrite or skip per folder," not "data corruption." `find_conflict_batches` library is ready; only the wizard page is missing. Spec §17 compliance is the goal.

**Status:** scoped — implementation pending. **Plan:** [`vault-v1-build-items.md#§5.H2`](vault-v1-build-items.md#5h2--per-folder-import-conflict-resolution).
**Decision:** build the per-folder conflict page for v1. Closes the spec §17 gap. Sized ~1 day.

---

### 8. §4.M1 — Orphan-chunk reaper *(design landed 2026-05-18 — new server endpoint + desktop reaper)*

**Why this slot:** bounded housekeeping. Each CAS-exhaust event leaks at most one batch's worth of chunks (~<100), and the existing 30-day server-side retention eventually reaps them. Blast radius is "wasted ciphertext quota until retention fires." Visible in test logs as "ghost bytes" — not a user-facing bug today.

**Status:** scoped — implementation pending.
**Decision:** add a new paginated `GET /api/vaults/{id}/chunks` server endpoint that lists every stored chunk_id; desktop computes `set(server_chunks) − set(manifest_chunk_refs)` and DELETEs the diff as a stage-0 housekeeping pass before stage 1 of [`vault-eviction-v1.md`](vault-eviction-v1.md). Rejected: server-side `KIND_RECLAIM_ORPHAN_CHUNKS` GC job (cleaner separation but more state-machine surface); accept-the-leak (the leak is bounded but visible in test logs as "ghost bytes"). Sized ~1 day server-side + ~1 day desktop-side.

**Implementation pointers:** server endpoint mirrors `batch_head_chunks` auth shape; pagination via `cursor` query param. Desktop reaper sits in `desktop/src/vault/ops/eviction.py` as a new `_reap_orphan_chunks` pre-stage that runs once per autosync cycle (not per-507) so the cost is amortized.

---

### 9. §6.H1 — Scheduled-purge auto-executor *(decided 2026-05-18 — fire-on-attended retained)*

**Why this slot:** resolved by ADR; no code work left. The decision *is* the fix. Listed here so the original review entry has visible disposition.

**Status:** resolved as documented decision. **ADR:** see [`architecture-decisions.md`](../architecture-decisions.md) `2026-05-18 — Scheduled-purge auto-executor stays fire-on-attended`.
**Decision:** keep the current fire-on-attended behaviour. Autosync notifies on due purges; user reopens Vault Settings → Danger zone and completes with the recovery kit. No new at-rest secret class (rejected option a), no new server scheduler infra (rejected option b). The partial fix already landed in `0b836aa` covers the user-facing half.

---

### 10. §5.M3 — Cross-subprocess fresh-unlock composition *(decided 2026-05-18 — per-subprocess scope is the v1 contract)*

**Why this slot:** resolved by ADR; no code work left. The decision *is* the fix. Listed here so the original review entry has visible disposition. Architecture doc / spec §13 will be amended to record the v1 tightening as a doc-only follow-up.

**Status:** resolved as documented decision. **ADR:** see [`architecture-decisions.md`](../architecture-decisions.md) `2026-05-18 — Fresh-unlock state is per-subprocess by design`.
**Decision:** per-subprocess state stays; sensitive ops chain only within a single subprocess. Cross-subprocess re-prompts are the intentional v1 tightening of spec §13. Rejected: on-disk timestamp file (option a) and POSIX shm (option b) — both broaden the read surface for local-process attacks under the same uid.

---

### 11. ~~§5.M2~~ — Migration runner shard genesis-insert for rev > 1 *(bundled into §5.C1)*

**Why this slot:** subordinate fix; ships with its parent build. No independent landing point.

**Status:** subordinate fix in §5.C1 migration wizard build. Implementation detail captured in [`vault-v1-build-items.md#§5.C1`](vault-v1-build-items.md#5c1--migration-wizard) under "Subordinate fixes bundled in this build."

---

### 12. ~~§5.M6~~ — Migration record `previous_relay_url` stale carry *(bundled into §5.C1)*

**Why this slot:** subordinate fix; ships with its parent build. No independent landing point.

**Status:** subordinate fix in §5.C1 migration wizard build. The chosen approach is option (a) — explicit `state.clear_previous_relay()` call at the start of every fresh start/verify/commit cycle. Implementation detail captured in [`vault-v1-build-items.md#§5.C1`](vault-v1-build-items.md#5c1--migration-wizard) under "Subordinate fixes bundled in this build."

---

## 2. Deferred Lows (polish-tier or verified-clean by reviewer)

The reviewer explicitly classified the rest of the Low section as acceptable-for-v1 or operator-deployment caveats. Listed here so they're not lost; not actively tracked as blockers.

### §1 (server) — operator caveats
- ~~**§1.L2**~~ — *(landed)* `guardMigrationTargetRelayUrl` rejects loopback / RFC 1918 / link-local hosts by default; operators opt in via `migrationAllowPrivateUrls` in `server/data/config.json`.
- ~~**§1.L3**~~ — *(landed as docstring)* shared-host PHP-FPM caveat captured in `VaultStorage::ensureDir`.

### §2 (desktop crypto / sync engine) — polish
- ~~**§2.L2**~~ — *(landed)* 50 ms × attempt backoff between CAS retries in `binding/sync.py`.
- ~~**§2.L3**~~ — *(landed as docstring)* `_imported_rename`'s 10_000 cap explained alongside the limit.

### §3 (sync engine internals)
- ~~**§3.L1**~~ — *(landed as docstring)* documented that pause deliberately preserves stubs; only disconnect orphans them.
- ~~**§3.L2**~~ — *(landed)* `baseline._walk_local` honours `ignore_dotfiles=True` to match `scan` / `preflight`; covered by `test_dotfiles_skipped_by_default_matches_scan_and_preflight`.
- **§3.L3** — Conflict-path random token is 32 bits; spec-compliant. *Reviewer noted as acceptable.*
- **§3.L4** — `MAX_OP_ATTEMPTS=10` ops sit in the queue forever; no permanent-failure UI surface. *Needs UI scoping (banner + per-op detail row + queue inspector); skipped autonomously.*

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
- ~~**§7.L1**~~ — *(landed)* `test_two_distinct_claimants_one_join_request_yields_200_and_409` pins the F-S13 CAS-on-pending shape with two distinct claimant devices + pubkeys.
- ~~**§7.L2**~~ — *(landed)* `test_case_distinct_paths_materialize_as_separate_files_on_linux` pins the case-sensitive contract; the case-insensitive-mount limitation is documented in the test body.
- ~~**§7.L3**~~ — *(landed)* negative-case `tamper` block schema documented in `tests/protocol/vault-v1/README.md`.

**Status of the actionable polish-tier entries:** 9 of 11 landed in this session (§1.L2, §1.L3, §2.L2, §2.L3, §3.L1, §3.L2, §7.L1, §7.L2, §7.L3). The two still-open: §3.L3 (reviewer-marked acceptable) and §3.L4 (needs UI scoping — banner + queue inspector for permanently-failed ops).

The 10 verified-clean / correction entries (§6.L1–L9) require no action.

---

## 3. Manifest-sharding step 7f cleanup *(complete except test-fixture sweep)*

Carried over from [`temp/finished-plans/vault-manifest-sharding.md`](../../temp/finished-plans/vault-manifest-sharding.md). Phases A → 7e shipped; step 7f did the heavy lifting (legacy `Vault.fetch_manifest` / `publish_manifest`, server `vault_manifests` table + `/manifest` endpoints, `FakeUploadRelay.put_manifest` / `get_manifest`, migration script + legacy fixture all gone).

**Status (verified 2026-05-18):** the sharded surface IS the production path. Steps §3.1–§3.6 below all landed; the only residual is a ~140-site test-fixture migration documented under §3.5 as a separate refactor (the kept-as-fixture helpers are intentional, not a regression). No functional gaps remain.

### ~~3.1 — Result-shape rename + `assemble_unified_manifest` callers~~ *(landed 878af94)*

Upload-result dataclasses now carry the sharded `(root, shard, remote_folder_id)` triple; the 7 upload-result-populating callers in `upload/folder.py` (4), `upload/single_file.py` (2), `upload/resume.py` (1) no longer call `assemble_unified_manifest`. Browser consumers synthesize at the `state.manifest =` assignment.

Remaining `assemble_unified_manifest` production callers (ops/delete.py 4 sites, remote_folders.py 1 site, binding/twoway.py 1 site, browser_model.py 2 sites) are not result-shape concerns — they are decode-only paths kept until `BrowserIndex` is shard-aware.

### ~~3.2 — Drop `Vault.fetch_unified_manifest`~~ *(skipped — premise invalidated, rationale in 77a2de4)*

The plan's "Recommended sequencing" item 2 ("compat synthesizer has no callers once §3.1 lands") was **wrong**: a fresh audit (2026-05-18) finds **17 production callers** spanning eviction, integrity, browser refresh, import wizard, folder runtime, remote-folders bootstrap. Most genuinely need the multi-folder unified view (browser state.manifest, eviction stage walks, sidebar refresh, …) — migrating each to inline `fetch_root_manifest + per-folder fetch_folder_shard + assemble_unified_manifest` would just **clone the method's body 17 times** with no benefit.

**Conclusion:** `Vault.fetch_unified_manifest` is a legitimate convenience method that assembles the multi-folder unified view from the sharded relay surface. It stays. Its docstring's "Phase H removes this method" line was amended, not removed.

### ~~3.3 — Protocol narrowing (`IntegrityVault`)~~ *(landed 62ce7a4)*

`IntegrityVault` Protocol slot trimmed to `(fetch_root_manifest, fetch_folder_shard)`. `_safe_fetch_manifest` in `desktop/src/vault/ops/integrity.py:322` reads root + each folder's shard and assembles a unified view inline so the existing chunk/version walks keep working against the assembled dict. `UploadVault` / `DeleteVault` Protocols already narrowed.

### ~~3.4 — Migrate last `find_file_entry` / `add_or_append_file_version` production callers~~ *(landed dad7297)*

`upload/conflict.py` + `import_/bundle.py` now use the shard-aware `_in_shard` variants. No production code outside `manifest.py` itself references the legacy helpers (verified by grep: `desktop/src/vault/upload/` and `desktop/src/vault/import_/` are clean).

### 3.5 — Legacy helpers in `desktop/src/vault/manifest.py` *(production-clean; test-fixture sweep deferred)*

**Landed (9617d22 + earlier):** `add_remote_folder` (manifest-level), `rename_remote_folder` (manifest-level), `add_or_append_file_version`, `merge_with_remote_head` all dropped.

**Still present (test-fixture only, intentional):** `make_manifest`, `make_remote_folder`, `tombstone_file_entry`, `find_file_entry` — kept because ~140 test sites still build/inspect unified manifests with them. Migrating the tests to a pure-sharded fixture vocabulary is a separate refactor and is the **only remaining open item** in §3.

`normalize_manifest_plaintext`, `canonical_manifest_json` stay — they shape envelope-serialization paths in production.

### ~~3.6 — Test-helper migration (`seed_sharded_state`)~~ *(landed 87b1ccb)*

`seed_sharded_state_from_manifest` and `mirror_legacy_from_sharded` replaced across the suite by the pure sharded `seed_sharded_state(vault, relay, *, vault_id=, remote_folders=, created_at=, author_device_id=)`. The "mirror" half is gone with the legacy `Vault.fetch_manifest` / `publish_manifest` declarations. Two surviving call-site mentions are pure documentation references (`tests/protocol/test_desktop_vault_upload.py:2165`, `tests/protocol/test_desktop_vault_delete.py:275`).

### 3.7 — Sequencing log

Items 1–6 from the original sequencing all landed in the order shown:

1. ~~§3.1 — `UploadResult.manifest` → `.root` + `.shard` + caller fanout~~ — `878af94`
2. ~~§3.2 — Drop `Vault.fetch_unified_manifest`~~ — skipped (`77a2de4`, rationale above)
3. ~~§3.3 — Narrow `IntegrityVault` Protocol~~ — `62ce7a4`
4. ~~§3.4 — Migrate `find_file_entry` / `add_or_append_file_version` production callers~~ — `dad7297`
5. ~~§3.6 — Test-helper mechanical sweep (`seed_sharded_state`)~~ — `87b1ccb`
6. ~~§3.5 — Drop unused legacy manifest helpers~~ — `9617d22` (production-side only; ~140-site test-fixture migration left as a separate refactor)

**Open:** the ~140-site test-fixture sweep that would let `make_manifest` / `make_remote_folder` / `tombstone_file_entry` / `find_file_entry` drop from `manifest.py`. Mechanical, non-blocking, ships independently.

---

## 4. Summary count

Reconciled 2026-05-18 after the design pass closed every "needs-design" item.

| Bucket | Total | Fully fixed | Design landed, impl pending | Doc decision (resolved) | Deferred Lows |
|---|---|---|---|---|---|
| Criticals | 17 | 16 | 1 (§5.C1) | 0 | 0 |
| Highs | 37 | 33 | 3 (§5.H2, §5.H3, §6.H3) | 1 (§6.H1) | 0 |
| Mediums | 35 | 31 | 3 (§4.M1, §5.M2, §5.M6) | 1 (§5.M3) | 0 |
| Lows | 24 | 4 | 0 | 0 | 20 |
| **Total** | **113** | **84** | **7** | **2** | **20** |

§5.M2 and §5.M6 are subordinate fixes bundled into the §5.C1 migration wizard build — counted once at the bucket level for visibility, but they share the parent's implementation path. §3.C1 + §6.H2 + §5.C2 fully landed on 2026-05-18 (see [`vault-eviction-v1.md`](vault-eviction-v1.md) + [`architecture-decisions.md`](../architecture-decisions.md) `2026-05-18 — Eviction policy` + entries 2 and 3 above).

### Breakdown of the 29 not-fully-fixed-by-code items

- **7 design-landed-pending-implementation** (§1 above): 1 Critical (§5.C1), 3 Highs (§5.H2, §5.H3, §6.H3), 3 Mediums (§4.M1, §5.M2, §5.M6). Each carries a plan-doc link. Implementation work is what's left.
- **2 doc-decision-resolved** (§1 above): §6.H1 (fire-on-attended), §5.M3 (per-subprocess fresh-unlock) — both captured in [`architecture-decisions.md`](../architecture-decisions.md) 2026-05-18 entries. No code needed; these are resolved by the decision itself.
- **20 deferred Lows** (§2 above): 2 §1 + 2 §2 + 4 §3 + 8 §6 verified-clean + 1 §6.L9 correction + 3 §7. Of these, the **11 actionable** items are §1.L2–L3 + §2.L2–L3 + §3.L1–L4 + §7.L1–L3.

User-facing math: **29 entries are not-yet-fully-fixed-by-code** — 7 design-pending + 2 doc-resolved + 20 deferred Lows. Of those, **27 are open work** (7 implementation + 20 deferred Lows); the 2 doc-decisions are effectively resolved.

---

## 5. Source of truth references

- **Max-effort review fixes landed:** [`temp/finished-plans/max-review-result.md`](../../temp/finished-plans/max-review-result.md) — every fixed item has a strikethrough heading + commit SHA + Approach paragraph.
- **Max-effort review fix log:** [`temp/finished-plans/max-review-result-progress.md`](../../temp/finished-plans/max-review-result-progress.md).
- **Historical doubts snapshot:** [`temp/finished-plans/review-doubts.md`](../../temp/finished-plans/review-doubts.md) — superseded by §1 of this file; kept for context strings that some code comments reference.
- **Manifest-sharding plan:** [`temp/finished-plans/vault-manifest-sharding.md`](../../temp/finished-plans/vault-manifest-sharding.md) — phases A → 7e + the bulk of 7f done; §3 of this file tracks the remaining cleanup.
- **This file:** the single live open-item index, kept in `docs/plans/` so it surfaces alongside active planning docs.
