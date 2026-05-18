# Vault v1 — outstanding follow-ups

Tracker for every item from the max-effort review (`temp/finished-plans/max-review-result.md`) that did NOT land as a code fix. Three categories:

1. **Needs-design** — fix scope requires explicit user decision before any code lands. Detail lives in [`temp/finished-plans/review-doubts.md`](../../temp/finished-plans/review-doubts.md).
2. **Partial** — primary risk closed but a follow-up gap remains.
3. **Deferred Low** — reviewer marked as polish-tier, acceptable for v1, or operator-deployment caveats.

Last reconciled against the archived tracker on 2026-05-17.

---

## 1. Needs-design (await user scoping)

These are new-feature builds or tradeoff decisions. The per-issue autonomous-fix protocol explicitly forbids building features without scoping; doubts file carries 2–3 resolution paths each.

| ID | Title | Why deferred |
|---|---|---|
| §3.C1 | Eviction stages 2/3 hard-purge: `purge_secret` UI | Admin-role gate landed; the spec also requires `purge_secret` + passphrase prompt on the 507-quota dialog. Three paths in doubts: passphrase prompt vs ADR amendment. |
| §5.C1 | Migration wizard does not exist | Library is wired; needs a multi-page GTK subprocess (~600 LOC). |
| §5.C2 | QR-join + grant approval UI | Library-only today; memory note says v1 ships recovery-kit-only. |
| §5.H2 | Per-folder import conflict resolution UI | Library has `find_conflict_batches`; needs a wizard page between Preview and Progress. Conservative `rename` default ships safely. |
| §5.H3 | Access-secret rotation client trigger | Library waits for callers; until rotation is wired, nothing breaks. Needs UI + server endpoint + kit-regeneration flow. |
| §6.H1 (partial) | Auto-execute scheduled hard-purges | Notification on due purges landed; auto-fire requires `purge_secret` persistence (three options: keyring, server cron, or attended-only). |
| §6.H2 | Revoke-device UI (Devices tab) | Server endpoints ship; desktop needs Devices tab + locked §3.3 wording + double-gate (~500 LOC). |
| §6.H3 (partial) | Export wizard | Sync-now wired to a real autosync kick; Export entry removed from tray pending the wizard build. |
| §4.M1 | Orphan-chunk reaper | Needs new `GET /chunks` endpoint OR server-side `KIND_RECLAIM_ORPHAN_CHUNKS` GC job. Leak bounded by 30-day retention. |
| §5.M2 | Migration runner shard rev > 1 bootstrap | Conditional on §5.C1. |
| §5.M3 | Cross-subprocess fresh-unlock | Current per-subprocess scope is MORE secure than spec implies. Three resolution paths in doubts. |
| §5.M6 | Migration `previous_relay_url` stale carry | Conditional on §5.C1 UX. |

**Count: 12 entries.** All detailed in `review-doubts.md` with verified-against file references and 2–3 resolution paths each.

---

## 2. Deferred Lows (polish-tier or verified-clean by reviewer)

The reviewer explicitly classified the rest of the Low section as acceptable-for-v1 or operator-deployment caveats. Listed here so they're not lost; not actively tracked as blockers.

### §1 (server)
- **§1.L2** — `migrationCommit` URL parser accepts private IPs / localhost. *Operator-config option; worth optional blocking-by-default with override.* `server/src/Controllers/VaultController.php:1465-1477`
- **§1.L3** — `VaultStorage::ensureDir` chmod 0700 inaccessible to other request pools on shared-host PHP-FPM. *Deployment caveat for shared hosting.* `server/src/VaultStorage.php:45`

### §2 (desktop crypto / sync engine)
- **§2.L2** — CAS retry loop has no overall livelock cap beyond `CAS_MAX_RETRIES`. *Under 10-device write storms a single device could exhaust budget per-folder forever. Log-volume sanity; suggested 50–100 ms × attempt backoff.* `desktop/src/vault/binding/sync.py:1058`
- **§2.L3** — `_imported_rename` caps at 10 000 collisions with `RuntimeError`. *Acceptable sanity bound; flagged for awareness.* `desktop/src/vault/manifest.py:861-866`

### §3 (sync engine internals)
- **§3.L1** — `BatchedUploadStub` orphans reaped only on disconnect, not pause. `desktop/src/vault/binding/lifecycle.py:319-341`
- **§3.L2** — Ignore-dotfiles flag inconsistent across `scan.py` / `preflight.py` / `baseline.py`.
- **§3.L3** — Conflict-path random token is 32 bits; spec-compliant. *Reviewer noted as acceptable.*
- **§3.L4** — `MAX_OP_ATTEMPTS=10` ops sit in the queue forever; no permanent-failure UI surface.

### §6 (desktop UI)
Reviewer marked all eight §6 Lows as **verified clean** or **acceptable for v1**. Listed for completeness only:
- **§6.L1** — AT-SPI label includes plaintext filenames. *Acceptable; same sensitivity as visible card title.*
- **§6.L2** — Rollback banner copy correctly mentions fresh-device limitation. *Spec satisfied.*
- **§6.L3** — Activity tab renders destructive events with humanised labels. *Spec satisfied.*
- **§6.L4** — Cross-window state sync is implicit-via-reload, no inotify. *Acceptable for v1.*
- **§6.L5** — No subprocess crash detection; tray doesn't offer to re-open. *UX rough edge.*
- **§6.L6** — `confirm_vault_clear_text_matches` uses `.strip().upper()`. *Matches spec.*
- **§6.L7** — Adw 1.4 fallback gate present via `dependency_check.py`. *Spec satisfied.*
- **§6.L8** — Wizard cancellation correctly preserves toggle. *Memory `feedback_respect_user_intent` satisfied.*

### §7 (test polish)
- **§7.L1** — QR claim race lower bound. *No test with two parallel claims with different pubkeys verifying exactly one 200 + one 409.*
- **§7.L2** — Case-insensitive local mount collision (`A.txt` vs `a.txt`) untested. *Linux primary; cross-mount filesystems (NFS, exFAT) still relevant.*
- **§7.L3** — Negative-vector README schema undocumented. *Add `tamper`, `envelope_byte_xor`, `aad_override`, `wrapped_key_byte_xor`, `decrypt_passphrase_override` to `tests/protocol/vault-v1/README.md`.*

**Count: 20 entries unfixed** (2 §1 + 2 §2 + 4 §3 + 8 §6.L1–L8 verified-clean + 1 §6.L9 correction + 3 §7 = 20). The 8 §6.L1–L8 items are pure verified-clean acknowledgements the reviewer flagged as "spec satisfied" or "acceptable for v1" — not real action items. The actionable polish-tier subset is therefore **11 = 2 + 2 + 4 + 3**, which is what most counts in this doc reference.

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

**12 in `review-doubts.md`** (await user scoping; each has 2–3 resolution paths):
- Criticals: §5.C1, §5.C2 (2 — new feature builds)
- Highs: §5.H2, §5.H3, §6.H2 (3 — new feature builds)
- Mediums: §4.M1, §5.M2, §5.M3, §5.M6 (4 — server-side feature OR conditional on §5.C1 OR security/UX tradeoff)
- Partials with follow-ups: §3.C1 (purge_secret UI), §6.H1 (auto-execute), §6.H3 (Export wizard) — these have *some* fix landed but the doubts file tracks the remainder. **3.**

Total in doubts file = 9 pure-new + 3 partials = 12 entries.

**17 deferred Lows** (reviewer-classified as polish-tier, verified-clean, or operator-deployment caveats — not actively tracked in doubts):
- §1.L2, §1.L3 (2 — server operator caveats)
- §2.L2, §2.L3 (2 — desktop sync polish)
- §3.L1–§3.L4 (4 — watcher/sync polish)
- §6.L1–§6.L8 (8 — reviewer marked verified-clean / acceptable for v1)
- §7.L1–§7.L3 (3 — test polish)

The user-facing math: **3 unfixed Highs + 4 unfixed Mediums + 20 unfixed Lows = 27 not-strikethrough items**, plus 2 unfixed Criticals = 29.

If you only count Lows the reviewer didn't mark as "verified clean" (i.e. the actionable polish-tier subset), that's **11 deferred Lows** (the 20 minus 8 §6 verified-clean minus the §6.L9 correction item which wasn't a real issue). That matches the count in §2 above.

---

## 4. Source of truth references

- Fixes landed: `temp/finished-plans/max-review-result.md` (every fixed item has a strikethrough heading + commit SHA + Approach paragraph)
- Append-only fix log: `temp/finished-plans/max-review-result-progress.md`
- Needs-design detail: `temp/finished-plans/review-doubts.md`
- This file: the open-item index, kept in `docs/plans/` so it surfaces alongside active planning docs
