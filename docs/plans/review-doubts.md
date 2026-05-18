# Vault v1 max-effort review — doubts & follow-ups

Entries logged by the autonomous fix workflow per `max-review-result.md`'s
per-issue protocol. Anything skipped, ambiguous, or only partially fixed
gets a record here.

---

## §3.C1 — Eviction stages 2/3 hard-purge: partial fix (admin gate only)

Status: partial-needs-followup
Date: 2026-05-17
Verified against: `server/src/Controllers/VaultController.php:1111-1284`,
`desktop/src/vault/ops/eviction.py:130-275`,
`desktop/src/windows_vault_browser/quota.py:30-150`
Doubt: Spec §D9 ("destructive-action ledger") requires hard-purges to
need **admin + purge_secret + fresh_unlock**. Eviction stages 2/3 now
require admin role (commit f621dc1) but still do NOT verify
`purge_secret` or surface a passphrase prompt. The 507-quota dialog in
`QuotaMixin._handle_quota_exceeded` is a one-click "Reclaim space"
confirmation; closing the gap requires a passphrase entry on that
dialog so the desktop can derive `purge_secret` and pass it to
`/api/vaults/{id}/gc/execute`. That UI change is meaningful enough
(brand-styled dialog + Argon2id-off-main-thread interaction with
§6.C1/§6.C2 fixes + retry path on wrong passphrase) that it deserves
explicit user scoping before landing.

Action taken: admin-role gate landed via new `KIND_FORCED_EVICTION`
plan kind and `purpose='forced_eviction'` body param on `gc/plan`.
Server enforces admin on both plan creation and execution. Desktop
threads `purpose` from stages 2/3.

Need from user: confirmation on whether to (a) add passphrase prompt
to the 507 dialog (closes spec gap, breaks today's one-click UX), or
(b) document acceptance in ADR + spec amendment leaving stages 2/3 at
admin-only.

---

## §5.C1 — Migration wizard UI

Status: skipped-needs-design (new feature build)
Date: 2026-05-17
Verified against: `desktop/src/windows_vault/tab_migration.py:54-63`
(button is `set_sensitive(False)`, comment "the engine is ready");
`desktop/src/vault/migration/runner.py` exports `run_migration` but
`grep -RE "run_migration\b" desktop/src/ | grep -v test` returns
only the runner's own definition + self-references — zero production
callers.
Doubt: The library (state machine, target bootstrap, verification,
on_committed callback, copy progress) is in place. What's missing is
a multi-page GTK wizard: source-relay target picker → preflight diff
→ progress/cancel page → verify confirmation → commit + switch-back
controls. That's a dedicated `windows_vault_migration.py` subprocess
with at least ~600 lines of UI + threading + AT-SPI labels and a
brand-styled stepper. Building UI surface autonomously violates the
per-issue protocol's "never build a new feature autonomously" rule.
Action taken: nothing; logged for explicit scoping.
Need from user: decision on (a) build the wizard as a single
follow-up PR (sized ~3-5 days), (b) ship v1 without migration UI
and document the engine as "library-only, v1.1 wizard", or (c) inline
a minimal "advanced URL entry" admin escape hatch that calls
`run_migration` directly without the wizard polish.

---

## §5.C2 — QR-join + grant approval UI

Status: skipped-needs-design (new feature build)
Date: 2026-05-17
Verified against: `desktop/src/vault/grant/qr.py:make_join_url`,
`desktop/src/vault/grant/wrap.py:wrap_grant_for_claimant /
unwrap_grant_for_claimant`, `parse_join_url` — all exported, zero
non-test callers in `desktop/src/`. Memory note
`project_vault_multi_device_story.md` already records that v1
multi-device is recovery-kit-only.
Doubt: Building this requires three new UI surfaces — claimant
"scan/paste join URL" view, admin "approve a join request" dialog,
orchestrator hitting `/join-requests/{req_id}/{claim,approve}`. Plus
a way for the claimant to surface a verification code that the admin
must read aloud. That's a multi-day feature build that the explicit
memory note says is **v1.x future work, not a v1 gap**.
Action taken: nothing; the recovery-kit + import-wizard path is the
shipping multi-device story.
Need from user: confirmation that QR-join can stay v1.1 (so this
Critical drops to "won't fix for v1.0, tracked elsewhere"), or
scoping for the build.

---

## §6.H1 — Scheduled-purge auto-executor needs purge_secret persistence

Status: partial-needs-followup
Date: 2026-05-17
Verified against: `desktop/src/vault/ops/purge_schedule.py:171-186`
(``build_execute_request_body`` requires ``purge_secret: str``);
``vaults.purge_token_hash`` BLOB column in
``server/migrations/002_vault.sql:32`` (server-optional, never set
by the desktop's create flow).
Doubt: To wire the autosync to literally call ``gc/execute`` on a
due purge, the desktop needs ``purge_secret`` in scope at the
moment the autosync fires. There are three paths and each has
real trade-offs:

  (a) Generate ``purge_token_hash`` at vault-create time, record
      ``purge_secret`` in the recovery kit, persist a keyring copy
      at schedule_purge time, read it from the keyring during
      autosync. Full automation — but the keyring stores a
      long-lived purge-fire credential, which is a new at-rest
      secret class. Need user buy-in.

  (b) Push the schedule to the relay as a real `KIND_SCHEDULED_PURGE`
      row with a server-side cron — server fires when due. Removes
      the desktop's "must be online" constraint entirely. But the
      relay currently has no scheduler infra; would need a small
      cron + retention policy.

  (c) Leave fire-on-attended (current behaviour with my partial
      fix): autosync notifies, user reopens Vault Settings →
      Danger zone, completes with the recovery kit. Dialog copy
      is now honest about this.

Action taken: partial fix in commit (this commit) — autosync
notifies on due purges, dialog copy clarifies the online
dependency. ``vault.purge.due_awaiting_user`` event documented.
The "auto-fire" half is still open.
Need from user: pick (a), (b), or (c). (c) ships as-is.

---

## §5.H2 — Per-folder import conflict resolution UI

Status: skipped-needs-design (new feature build)
Date: 2026-05-17
Verified against: `desktop/src/windows_vault_import.py:36, 364`
(`ImportMergeResolution(per_folder={})` always — module docstring
admits "Conflict-resolution UI is not yet wired here");
`desktop/src/vault/import_/conflicts.find_conflict_batches` is the
library function with zero non-test callers in `desktop/src/`.
Doubt: The wizard currently defaults to `rename` (the conservative,
data-loss-free option) for every conflicting folder, so the failure
mode is "user can't pick overwrite or skip per folder" rather than
"data corruption". Spec §17 calls for per-folder conflict batches
with an "Apply to remaining" button — that's a new wizard page
between Preview and Progress with N controls + a "Apply to all
remaining folders with the same conflict kind" affordance.
Building that page autonomously violates the per-issue protocol's
"never build a new feature autonomously" rule (~300 LOC of GTK +
threading + AT-SPI labels + brand styling).
Action taken: nothing; the conservative default (rename) keeps the
shipping behaviour safe. The library is ready.
Need from user: decision on (a) build the per-folder conflict
page as a follow-up PR, (b) ship v1 with the rename-only default
and document the gap as "v1.1 enhancement", or (c) inline a single
global picker on the Preview page (rename / overwrite / skip for
the whole import) as a minimal step before the per-folder UI.

---

## §6.H2 — Revoke-device UI (entire Devices tab)

Status: skipped-needs-design (new feature build)
Date: 2026-05-17
Verified against: `desktop/src/windows_vault/main_window.py:188-207`
(four tabs — devices, security, sync_safety, storage — are literal
"This panel is reserved for later development" placeholders);
`server/src/Controllers/VaultGrantsController.php:420` ships
`revokeDeviceGrant` + `listGrants` endpoints; `grep -r "Revoking
this device" desktop/` returns empty.
Doubt: The server endpoints (revoke + list active/revoked grants)
are shipped and tested. The desktop side has zero library wrappers
calling them, and the Devices tab is a placeholder. Building a real
Revoke UI requires:
  - A `list_device_grants` / `revoke_device_grant` client helper
    (HTTP adapter + typed responses + retry/auth glue).
  - GTK page listing active grants in a card-per-row layout with
    a per-row Revoke button, "last seen", device_name attribution.
  - Locked confirmation copy verbatim per §3.3: "Revoking this
    device prevents future Vault access. It cannot erase data
    already copied to that device." A locked-string source-pin
    test so future copy edits don't regress the wording.
  - Fresh-unlock + admin-role double-gate (existing pattern from
    `tab_danger.py`).
  - Reactive refresh of the row list after a successful revoke.
A v1 vault that can grant device access but cannot revoke it has
no defence against a lost paired desktop — this is the heaviest
v1 gap of the §6 batch. Building the surface autonomously violates
the per-issue protocol's "never build a new feature autonomously"
rule (~500 LOC of GTK + HTTP + tests + brand styling).
Action taken: nothing; the spec gap remains open.
Need from user: decision on (a) build the Devices tab as a
follow-up PR (sized ~2-3 days; mirrors the Folders tab's shape
with the destructive-action gate pattern from tab_danger), or
(b) ship v1 with revoke as a CLI helper / direct curl against
the server endpoint and document the desktop-UI gap as a v1.x
target.

---

## §6.H3 (partial) — Tray Export entry removed pending wizard build

Status: removed-needs-design (new feature build)
Date: 2026-05-17
Verified against: `desktop/src/vault/ui/ui_state.py:101` (tokens list
no longer includes `"export"`); `desktop/src/tray/vault_submenu.py`
(the `_vault_export_stub` method is gone, the menu item is gone);
`desktop/src/vault/export/bundle.py:write_export_bundle` is the
shipped data-layer entry point with zero non-test callers in
`desktop/src/`.
Doubt: Wiring "Sync now" to actually fire the autosync was a one-
liner — the in-process loop already existed. Wiring "Export…" would
require a brand-new GTK subprocess (`windows_vault_export.py`):
path picker, passphrase entry + confirm + strength meter, Argon2id-
off-main-thread progress with the §6.C* worker pattern, optional
"shred bundle after copying" toggle, success screen with "Verify
bundle" action. That's the kind of feature build the per-issue
protocol says not to do autonomously.
Action taken: tray entry removed (review §6.H3) so it no longer
fires theatre notifications. The Sync-now half landed as a real
fix.
Need from user: decision on (a) build the export wizard as a
follow-up PR (sized ~1-2 days; mirrors the import wizard's shape),
(b) ship v1 with the recovery-kit path as the only recovery surface
(memory `project_vault_multi_device_story` already says this is
the v1 multi-device story), or (c) inline a minimal "Export to
file…" CLI helper as a power-user escape hatch.

---

## §4.M1 — Orphan-chunk stage-0 reaper

Status: skipped-needs-design (requires server-side support)
Date: 2026-05-17
Verified against: `desktop/src/vault/upload/folder.py:436-519` (the
``_publish_batch_with_cas_retry`` exhaust path raises after CAS
retries, leaving any chunks already uploaded as orphans);
`desktop/src/vault/ops/eviction.py` (stage 1 only purges chunks
behind expired tombstones, not active orphans);
`server/src/Controllers/VaultController.php` (no endpoint exists
that lists all chunk_ids for a vault, so the desktop can't compute
the orphan set without enumerating the manifest's chunk references
against a server-side inventory).

Doubt: The review's suggested fix — a "stage-0 orphan-chunk reaper
that does ``batch-head × manifest references`` and deletes the
difference" — requires the desktop to know every chunk currently
stored against the vault. The only existing endpoint is
``batch_head_chunks`` which takes a list and returns presence; it
doesn't enumerate. Building the reaper requires either:

  (a) A new server endpoint ``GET /api/vaults/{id}/chunks`` that
      lists every stored chunk_id (paginated). Desktop then computes
      ``set(server_chunks) − set(manifest_chunk_refs)`` and DELETEs
      the diff. Simple but adds a new API surface that needs auth +
      pagination + rate-limit thought.

  (b) Move the reaper to the server. The GC job kind already has
      ``KIND_RECLAIM_AGED_TOMBSTONES`` (or similar); a new
      ``KIND_RECLAIM_ORPHAN_CHUNKS`` would walk the chunks table,
      cross-check against the manifest references, and delete. The
      desktop just triggers the job. Cleaner separation but needs a
      new state machine + admin gate.

Neither path is a Medium-scope fix. The active-orphan leak is real
but bounded: each CAS-exhaust event leaks at most one batch's worth
of chunks (typically <100), and the existing 30-day chunk retention
policy (server-side) will eventually reap them. The leak's blast
radius is "wasted ciphertext quota until retention fires".

Action taken: nothing in code. Document the leak shape here so the
next eviction-pipeline session has a concrete starting point.

Need from user: decision on (a) ship the new ``GET /chunks`` +
desktop reaper, (b) push the work to a server-side
``KIND_RECLAIM_ORPHAN_CHUNKS`` GC job, or (c) accept the leak as
"bounded by retention" and document it in the architecture doc as
a known v1 limitation.

---

## §5.H3 — Access-secret rotation has no client trigger

Status: skipped-needs-design (new feature build)
Date: 2026-05-17
Verified against: `desktop/src/vault/grant/access_rotation.py:65-110`
(`generate_new_secret`, `rotation_request_body`, reminders all
exported; zero non-test callers in `desktop/src/`);
`desktop/src/windows_vault/tab_recovery.py:56` tooltip reads
"Recovery-material rotation is not implemented yet".
Doubt: Until rotation is wired, nothing breaks — the library waits
for callers. This is a pre-emptive risk: when rotation lands, every
existing recovery kit becomes silently undecryptable on the relay
side (right master_key, wrong bearer), so the wizard has to
prompt for kit regeneration in the same flow. Building the trigger
requires (a) a "Rotate access secret" button under Settings →
Recovery, (b) a confirmation dialog explaining "this invalidates
your existing recovery kits", (c) the post-rotation recovery-kit
regeneration step, (d) a server-side `/rotate` endpoint + auth
hooks. (d) is also missing today.
Action taken: nothing; current shipping behaviour is "no rotation"
which is safe pending the build.
Need from user: decision on whether v1 ships without rotation
(documented as v1.x), or scope a build that bundles UI + server
endpoint + kit-regeneration prompt together.
