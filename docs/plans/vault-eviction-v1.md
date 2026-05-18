# Vault eviction v1 — age-ordered auto-purge with quota-shrink passphrase gate

**Status:** decided 2026-05-18; supersedes the §3.C1 needs-design framing in [`unfinished.md`](unfinished.md). Implementation pending.

**Code anchor:** `desktop/src/vault/ops/eviction.py`, `desktop/src/windows_vault_browser/quota.py`, `desktop/src/vault/relay_errors.py`, `docs/architecture-decisions.md`.

## Decision

Replace today's tier-by-tier eviction (stages 1 → 2 → 3) with a two-track policy:

1. **Stage 1 unchanged** — expired tombstones, always safe, auto-runs on every sync pass.
2. **Stages 2 + 3 merge into a single "age-ordered destructive purge"** — unexpired tombstones and oldest historical versions of multi-version live files combined into one oldest-first iterator. Auto-runs when an upload won't fit. Bounded to "free exactly enough for THIS upload."
3. **Alarm gate**: if the relay ever reports `used_bytes > quota_bytes`, suspend all uploads and require passphrase before any destructive purge.

The desired user-visible behavior: "Use all of my 1 GB quota for versioning and recoverability. Trim oldest stuff when I need to. Pause and ask me if something looks wrong."

## Threat model

The destructive purge in stages 2 + 3 can be weaponized in three ways:

- **Compromised sync-only device tries to mass-purge recoverable data.** *Defended by* the existing admin-role gate that landed in `f621dc1` (`KIND_FORCED_EVICTION` plan kind; server enforces `role=admin` on both `gc/plan` and `gc/execute`). A sync-only device cannot trigger the destructive purge at all.
- **Compromised relay / admin shrinks quota.** Forces paired devices to delete data to "fit" a smaller cap. *Defended by this policy's alarm condition.* Under normal operation `used > quota` is impossible (the server denies overflow at init), so observing it = tampering. Passphrase required before any cleanup.
- **Borrowed / physically unlocked admin laptop.** *Partially defended:* the auto-purge is scoped to a single upload's bytes — cannot be used to mass-delete; can only free enough for one specific upload's worth of writes. Full defense (passphrase on every purge) was rejected as too disruptive for the v1 UX.

What we **deliberately** don't defend against here:

- **A relay that under-reports `used_bytes`.** The client trusts the relay's quota signal. A consistently-lying relay could lull the client past quota without the client ever noticing. Out of scope for v1 — would require a client-side ledger of every chunk ever committed and per-vault size accounting independent of the server. Document the gap in the ADR.
- **The "first contact" attack** where a fresh device sees a deliberately-misreported `used` < `quota` at onboarding. The alarm condition is stateless (just `used > quota` at each check), so there's no fresh-device baseline to spoof. The lying-relay gap above covers this too.

## Algorithm

### Normal upload path (auto-purge)

On every upload-init attempt that returns **507 `vault_quota_exceeded`**:

```
quota = response.quota_ciphertext_bytes
used  = response.used_ciphertext_bytes
size  = upload.projected_bytes

if used > quota:
    -> ALARM (see below)
elif used + size > quota:
    while used + size > quota:
        candidate = next_oldest_destructive_candidate()
        purge(candidate)
        used -= candidate.freed_bytes
    retry_upload_init()
else:
    # Stale 507; just retry.
    retry_upload_init()
```

**Candidate ordering** — single iterator combining:

- Unexpired-tombstone chunks (currently inside the 30-day recoverable grace window), sorted by `entry.deleted_at`.
- Oldest-version chunks of multi-version live files (every version except the latest), sorted by `version.created_at`.

Interleaved oldest-first across both sources. A 6-month-old v1 of a still-live file is purged **before** a 3-day-old Trash entry — matches the "drop the staleest data first" mental model and preserves recently-deleted-but-recoverable files longer.

**No batching, no slack.** `target_bytes_to_free` equals exactly the amount needed for this upload's projected bytes. The loop stops as soon as one more deletion brings the upload into the quota envelope, even if subsequent uploads in the queue might also need space (they'll trigger their own purges when their init fires). This maximizes preserved recoverability and avoids over-purging.

**No passphrase, no UI prompt for this path.** The upload-init retry is transparent. The user sees the upload pause briefly then succeed.

### Alarm path (`used > quota` observed)

When any 507 response shows `used > quota`:

1. **Suspend all uploads** for this vault (queued and in-flight at the next safe boundary). The upload that triggered the alarm is paused, not failed.
2. **Notify the user** (toast + sticky banner on the vault home): "Vault quota was reduced from X to Y. One-time cleanup needs approval."
3. **Open the brand-styled cleanup dialog** with a passphrase entry. Spec'd copy: "The relay reports the vault is now over capacity (used Y, quota X). This can happen if the relay quota was reduced. Type your passphrase to authorize a one-time cleanup that brings stored data back under quota."
4. **On unlock**: derive a fresh `purge_secret` from the passphrase, run the age-ordered destructive purge until `used ≤ quota`, then resume uploads. The destructive purge in alarm mode is **not** scoped to a single upload — it brings the whole vault back under the new (smaller) quota in one pass.
5. **On cancel**: uploads stay suspended. User can retry from Vault Settings → Storage (a new "Approve cleanup" button surfaces while suspended), raise the quota on the server, migrate to another relay, etc.
6. The alarm fires once per shrink event — subsequent uploads in the same session don't re-prompt, because after step 4 the invariant `used ≤ quota` is restored.

### Server-side guard (unchanged)

The server's existing init-deny path stays exactly as today:

- Vault upload init projects `used + upload_size`. If `> quota`, returns **507** with `used_ciphertext_bytes` + `quota_ciphertext_bytes`.
- This is the **only** safeguard against a desktop client that bypasses the client-side rules. Even a malicious client cannot push the vault past quota.

## UX

| Scenario | Today | v1 |
|---|---|---|
| Quota stable, upload doesn't fit | "Reclaim space" dialog, one-click confirm, stages 1→2→3 tier-by-tier | Transparent auto-purge; no dialog. Upload pauses briefly, then succeeds. |
| Quota shrank (`used > quota` observed) | No detection — stages 2/3 still auto-run and over-destroy to fit the new cap | Uploads suspended. Sticky banner + passphrase dialog. User approves once → cleanup runs → uploads resume. |
| No destructive candidates left | "Vault is full and no backup history remains" terminal banner | Same banner. Upload fails with `quota_exhausted`. |

## Implementation pointers

**Code that changes:**

- `desktop/src/vault/ops/eviction.py` — collapse `_unexpired_tombstone_candidates` and `_oldest_version_candidates` into a single `_destructive_candidates_oldest_first` iterator. Remove the separate stage_2 / stage_3 cascade. Stage 1 (`_expired_tombstone_candidates`) stays as a pre-step. The merged iterator's sort key is `min(entry.deleted_at, version.created_at)` depending on candidate type.
- `desktop/src/windows_vault_browser/quota.py::_handle_quota_exceeded` — replace today's "Reclaim space" one-click dialog with two paths:
  - Silent auto-purge (no dialog) for `used ≤ quota` and `used + size > quota`.
  - Brand-styled alarm dialog for `used > quota`.
- `desktop/src/vault/ops/purge_schedule.py` — `purge_secret` derivation logic is reused for the alarm path. The scheduled-purge runner stays for its own use case (§6.H1).
- `desktop/src/vault/upload/errors.py::describe_quota_exceeded` — add an `alarm: bool` field to the returned dict; update the heading + body strings accordingly.

**Code that stays:**

- Admin-role gate (`KIND_FORCED_EVICTION` plan kind, `purpose='forced_eviction'` body param). Both auto-purge and alarm-purge still go through the admin-gated `/gc/execute` endpoint.
- Server-side `vault_quota_exceeded` 507 contract.
- Stage 1 expired-tombstone housekeeping.
- The terminal "no backup history remains" banner for the empty-candidates case.

**Event vocabulary** (additions for `docs/diagnostics.events.md`):

- `vault.eviction.auto_purged_oldest vault=… path=… version_id=… freed_bytes=…` — replaces today's separate `tombstone_purged_early` / `version_purged` for the auto-purge path. One event per candidate deletion.
- `vault.eviction.alarm_used_exceeds_quota vault=… used=… quota=…` — fires when the alarm trips.
- `vault.eviction.alarm_purged_oldest vault=… path=… version_id=… freed_bytes=…` — destructive deletions inside the alarm-approved cleanup. Separate event so the audit log distinguishes "auto-purged for fitting an upload" from "alarm cleanup after detected shrink".

**ADR entry to add** (`docs/architecture-decisions.md`):

- `2026-05-18 — Eviction policy: age-ordered auto-purge + alarm on used>quota`. Records the threat model and the deliberate-non-decisions (no client-side ledger, no defense against a lying relay).

## Open implementation sub-questions

Small specifics to nail down during implementation; none change the design above:

1. **Proactive vs reactive alarm check.** Does the desktop poll `used` / `quota` on connect / Vault Settings open, or only check on 507 responses? *Recommendation: reactive in v1 (simpler, fewer endpoints), add proactive in a follow-up if the lag turns out to matter.*
2. **What counts as a "single upload" for the auto-purge bound?** If the user drops a folder of 1000 files, do we purge per file or per folder batch? *Recommendation: per `upload-init` call — folder uploads already aggregate at init time so this falls out for free.*
3. **What if multi-version purge would drop the only version of a still-live file?** It can't — the iterator excludes `latest_version_id` for every live entry. This is a confirmation, not an open question.
4. **What if the alarm fires during a long-running sync?** Suspend queued uploads at the next poll boundary, not mid-chunk. The sync engine already has pause primitives (`SyncCancelledError`).
5. **Should `used` be re-read after each candidate deletion, or trusted as `used -= freed_bytes`?** Trust the local arithmetic; re-poll only after the whole loop finishes, before retry_init. Extra round-trips per candidate would balloon the latency for no real benefit.

## Test coverage to add

When implementation lands:

- `test_auto_purge_drains_oldest_until_upload_fits` — single upload triggers 507; iterator drains oldest tombstone + version chunks; loop stops at exact-fit boundary.
- `test_auto_purge_excludes_latest_versions` — never purges the only live version of a file.
- `test_alarm_fires_when_used_exceeds_quota` — `used > quota` in 507 → suspends uploads, no auto-purge runs, alarm dialog shown.
- `test_alarm_cleanup_purges_until_used_under_quota` — after passphrase unlock, alarm cleanup brings `used ≤ quota` and resumes uploads.
- `test_no_destructive_candidates_terminal_banner` — when iterator empty, falls through to the existing "no backup history remains" path.
- `test_compromised_sync_device_cannot_trigger_purge` — non-admin role gets 403 from `gc/execute` (regression-pin the existing admin-role gate).
