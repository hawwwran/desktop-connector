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
