# Activity timeline — wire the producer side

## Context

The Vault Settings → Activity tab is **wired but inert**: F-501 built the
consumer half (`desktop/src/windows_vault/tab_activity.py`,
`desktop/src/vault/state/activity.py`, plus `operation_log_tail` schema
fields on root + shard envelopes), but **no code anywhere appends to
`operation_log_tail`** when uploads, deletes, restores, grants, evictions,
purges, migrations, or rotations happen. The schema is plumbed end-to-end:
`make_root_envelope` (manifest.py:527/547), `make_folder_shard`
(manifest.py:598/614), and `assemble_unified_manifest` (manifest.py:811)
all carry the field through with `[]` defaults. The 16 entries in
`_EVENT_TYPE_LABELS` exist as `log.info(...)` strings at producer sites
but never become structured rows on the encrypted manifest. The
server-side `vault_audit_events` table was retired in F-S16
(`Database.php:169` does `DROP TABLE IF EXISTS`), leaving the encrypted
op-log as the **only** intended source — and it's empty.

User-visible symptom: Activity tab always reads "No activity yet. Once
you upload, delete, or grant access, entries will appear here." It is
trivially passing the T17 wiring tests (grep-based source-pins at
`tests/protocol/test_desktop_vault_t17_wired_source.py`), which only
check that the UI strings exist — they never construct a real entry.

Intended outcome: every vault operation that changes user-visible state
lands a structured row on the next manifest publish, so the Activity tab
becomes a real audit timeline (with filename filter, kind grouping,
device attribution, revision anchor). Other devices fetching the
manifest see the same timeline — the field is encrypted and travels
with the manifest like every other state.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Append per-event entries to `operation_log_tail` on the **shard** for file ops, on the **root** for vault-wide ops. | Schema split (manifest.py:527 root, 598 shard) was designed for this. A device with a per-folder grant only fetches that one shard; root-scoped events on a shard it can't decrypt would be unreadable. |
| D2 | Modify `assemble_unified_manifest` (manifest.py:811) to merge root + per-shard `operation_log_tail` into the unified view, sorted by `(ts, author_device_id, revision)`. | Today shards' tails are dropped on assembly — the Activity tab would see nothing even with producers wired. Tie-break needed because burst uploads from one device share a wall-clock second. |
| D3 | `MAX_OP_LOG_TAIL = 200` entries. Drop-oldest when over cap. No `archived_op_segments` rotation in v1. | One `PUBLISH_BATCH_SIZE=50` batch fills 25 % of the tail — comfortable headroom. ~230 B per entry × 200 = ~46 KB plaintext (~50 KB AEAD). Pin cap as ≥ `4 × PUBLISH_BATCH_SIZE` via comment so future tuning is obvious. Archived-segments rotation is genuine work (separate ciphertext segments + fetch path) — defer to v1.1. |
| D4 | When the tail is full and would drop entries, emit a `vault.activity.tail_truncated_evicted_oldest count=N` INFO log and surface a "Showing most recent 200 events" hint on the Activity tab status label. | Drop-oldest is silent by default; users will not intuit that history rolls. Two cheap mitigations preserve observability without rotation. |
| D5 | Genesis `vault.create` lands on **the first follow-up root publish**, not the genesis envelope. | `Vault.prepare` (vault.py:302–315) builds genesis with hardcoded `operation_log_tail=[]`. Patching genesis to carry one entry is a schema-version concern; deferring to first-post-genesis publish keeps the change surgical. Visible cost: the very first row in a brand-new vault's timeline is the *second* root revision, not the first. Acceptable. |
| D6 | Skip op-log appends on **no-op replay paths** (e.g., `_merge_batch_into_shard_with_bump` reapplying a tombstone to an already-tombstoned entry). | Otherwise CAS retries duplicate entries for ops that did no work on the retry pass. Implementation: have `tombstone_file_entry_in_shard` / `add_or_append_file_version_in_shard` return a sentinel signaling "no mutation"; the op-log helper skips on that. |
| D7 | **CAS retries must preserve the server tail** read on retry, not the original-attempt tail. | `_merge_batch_into_shard_with_bump` (sync.py:1134) and `_publish_folder_purge_with_retry` (eviction.py:451) rebuild candidate from the server-side shard on 409. The op-log helper must take `prior_tail` explicitly so concurrent producer entries from the other writer aren't dropped. |
| D8 | `vault.purge.executed` is **shard-scoped** (one entry per affected folder), not root-scoped. `vault.purge.scheduled` / `vault.purge.cancelled` stay local (not in the manifest). | Schedule / cancel are local state — they never commit to the relay. Execute fires per-folder shard publishes; the entry naturally lives on the shard it tombstones. |

## Critical files

### Producer plumbing (new)
- **`desktop/src/vault/state/op_log.py`** (new) — `build_op_log_entry`, `MAX_OP_LOG_TAIL`, `append_op_log_entries(prior_tail, new_entries)` with drop-oldest + truncation-log emission.

### Shard-scoped producer wiring
- `desktop/src/vault/binding/sync.py` — `_apply_batch_to_shard` (lines 926–989) builds N entries per batch; `_merge_batch_into_shard_with_bump` (lines 1134–1177) re-applies on CAS retry using **server tail**.
- `desktop/src/vault/upload/single_file.py` — line 738 publish call (single-file path that bypasses the batch loop).
- `desktop/src/vault/upload/folder.py` — line 485 publish call (folder-upload batch path).
- `desktop/src/vault/ops/delete.py` — lines 101 / 153 (single + folder delete); lines 233 / 359 (restore); line 398 `_publish_shard_with_retry`.
- `desktop/src/vault/ops/eviction.py` — line 227 (`_run_stage`); line 451 (`_publish_folder_purge_with_retry`). Eviction events: `auto_purged_oldest`, `alarm_purged_oldest`, `tombstone_purged_expired`.
- `desktop/src/vault/ops/clear.py` — `delete_folder_contents` (lines 59–82) for `vault.folder.cleared` per-folder entries.
- `desktop/src/vault/ops/purge_schedule.py` — `vault.purge.executed` per-folder appends (currently only writes to `vault_pending_purges.json`).

### Root-scoped producer wiring
- `desktop/src/vault/ops/clear.py` — lines 84–154 `clear_vault` for `vault.vault.cleared` root entry.
- `desktop/src/vault/migration/runner.py` — line 315 commit publish for `vault.migration.committed` root entry.
- `desktop/src/vault/grants/issue.py` (or whichever owns the grant publish) — `vault.grant.created` root entry. **Producer log line is missing today** — add it alongside the entry.
- `desktop/src/vault/grants/client.py` line 123 (`revoke_device_grant`) — `vault.revoke.completed` root entry. **Producer log line missing.**
- `desktop/src/vault/grants/rotate_client.py` (and/or `access_rotation.py`) — `vault.rotation.completed` root entry. **Producer log line missing.**

### Consumer-side wiring (the unified-view merge)
- `desktop/src/vault/manifest.py:783–831` — `assemble_unified_manifest`: merge root + every shard's `operation_log_tail` into the returned `unified["operation_log_tail"]`, sorted by `(ts, author_device_id, revision)`.
- `desktop/src/vault/state/activity.py:210–222` — dedup key (currently `(ts, event_type, device_id)` — drops `path` deliberately). Add the merged shard entries; verify dedup still picks the richer entry when root + shard accidentally carry the same event (shouldn't happen with D1's split, but defensive).

### Collateral bug fixes (bundled per phase)
- `desktop/src/vault/ops/delete.py:359` — folder-restore logs `vault.restore.folder_completed`; rename to `vault.restore.completed` to match `_EVENT_TYPE_LABELS` (the labels map is the wire contract).
- `desktop/src/vault/ops/clear.py` — emit `vault.folder.cleared` log per cleared folder.
- `desktop/src/vault/ops/eviction.py` — emit `vault.eviction.alarm_used_exceeds_quota` on the alarm path's "vault still over quota after cleanup attempt" branch.
- Add missing log emissions: `grant.created`, `revoke.completed`, `rotation.completed`.

### ADR
- `docs/architecture-decisions.md` — dated 2026-05-19 entry recording D1–D8.

### Tests
- `tests/protocol/test_desktop_vault_op_log.py` (new) — producer-side unit tests:
  - `build_op_log_entry` shape (matches `normalize_op_log_entry` parsing).
  - `append_op_log_entries` truncation, ordering, prior-tail preservation.
  - One test per event type × scope (shard vs root): "after running op X against a fake relay, fetched manifest's op-log tail contains an entry with the expected `type` / `path` / `device_id` / `revision`."
  - CAS-retry replay: simulate a 409 conflict on shard publish; assert no duplicate entries and the server-side concurrent entries survive.
- `tests/protocol/test_desktop_vault_manifest.py` — extend with `assemble_unified_manifest` shard-tail merge test (ordering + tie-break).
- `tests/protocol/test_desktop_vault_t17_wired_source.py` — add producer-side anchor: `assertIn("build_op_log_entry", <relevant source>)`. Source-pin only; the real coverage lives in the new test file.

## Implementation outline

Phased so each phase is shippable on its own and the Activity tab gets
incrementally useful entries.

### Phase 1 — Producer plumbing + merge fix (foundation)
1. Write `state/op_log.py` with `MAX_OP_LOG_TAIL`, `build_op_log_entry`, `append_op_log_entries`.
2. Fix `assemble_unified_manifest` to merge shard tails (with tie-break sort).
3. Unit tests for both.

### Phase 2 — Shard-scoped events (highest-impact daily flow)
4. Wire upload (`upload/single_file.py`, `upload/folder.py`, batch path in `sync.py`).
5. Wire delete (`ops/delete.py` — single + folder).
6. Wire restore (`ops/delete.py` — version + folder; rename `folder_completed` → `completed`).
7. Wire eviction (`ops/eviction.py` — three event types; add the missing `alarm_used_exceeds_quota` emission on the alarm path).
8. Add the missing `vault.folder.cleared` log emission inside `ops/clear.py:delete_folder_contents`.
9. CAS-retry preservation: thread `prior_tail` through `_merge_batch_into_shard_with_bump` and `_publish_folder_purge_with_retry`. Skip on no-op replay (D6).

### Phase 3 — Root-scoped events (less-frequent ops + missing emissions)
10. Add missing log emissions: `grant.created`, `revoke.completed`, `rotation.completed`. **Landed.**
11. Wire `vault.create` on first follow-up root publish (D5). **Deferred** — see "Phase 3 deferred" below.
12. Wire `vault.{vault.cleared, migration.committed, purge.executed, grant.created, revoke.completed, rotation.completed}` and the root-scoped `eviction.alarm_used_exceeds_quota` entry. **Partial** — only `vault.vault.cleared` lands an op-log entry in this phase (one extra root publish at the end of `clear_vault`). The remaining six events all need new follow-up root publishes attached to relay-side state changes that don't otherwise touch the manifest; deferring them keeps the producer-side change footprint surgical.
13. Truncation observability: log line + UI status hint (D4). **Landed.**

### Phase 3 deferred → Phase 3.1 follow-up

These items were named in Phase 3 but require structural changes (an extra root publish per op, on relay paths that don't currently publish manifests). Bundling them together as a follow-up keeps each change traceable rather than smearing six new round-trips across as many commits.

- **`vault.create` on first follow-up root publish (D5).** Needs a one-shot "first revision after genesis" detector at every root-bump call site, or a dedicated post-genesis publish from `Vault.prepare`. Cosmetic — the first row in a new vault's timeline currently starts at the second revision.
- **`vault.migration.committed` op-log entry.** `migration/runner.py:307 migration_commit` is a relay call that hands off the vault to the target relay; the desktop doesn't publish a manifest revision in the commit step. Wiring an entry would mean a fresh root publish on the target relay post-switch.
- **`vault.eviction.alarm_used_exceeds_quota` op-log entry.** The log emission already exists at `windows_vault_browser/quota.py:104`. Wiring an op-log row would mean a root publish before the alarm cleanup pass — extra cost on a stress path.
- **`vault.grant.created` / `revoke.completed` / `rotation.completed` op-log entries.** Grant lifecycle is server-side state (server tables, not manifest fields). Adding entries means each grant/revoke/rotation triggers an extra follow-up root publish. Defensible but adds latency to user-facing flows.
- **`vault.purge.*` op-log entries.** The scheduled-purge feature is fire-on-attended per ADR 2026-05-18 and `mark_purge_executed` has no current callers in the desktop tree. Wire op-log when the scheduled-purge auto-executor lands (currently out of scope).

### Phase 4 — Stabilization
14. Integration test against the `FakeShardedRelay` harness: upload → fetch_unified_manifest → assert presence. **Landed** — `FetchUnifiedManifestIntegrationTests` in `tests/protocol/test_desktop_vault_binding_batched_publish.py`.
15. T17 producer-side source-pin update. **Landed** — `test_producer_side_wires_op_log_append` + `test_unified_merge_includes_shard_tails` in `tests/protocol/test_desktop_vault_t17_wired_source.py`.
16. ADR entry. **Landed** — `docs/architecture-decisions.md` 2026-05-19 entry recording D1–D8.
17. Live-twin smoke test: drive a few uploads + a delete + a grant on the dev twin, open the Activity tab, screenshot the populated timeline. **Recipe landed** — `docs/testing/vault-tests.md` Test 10. Run interactively via the existing chained-test pattern when ready.

## Verification

End-to-end after each phase:

- **Unit**: `cd desktop && python3 -m pytest tests/protocol/test_desktop_vault_op_log.py tests/protocol/test_desktop_vault_manifest.py tests/protocol/test_desktop_vault_t17_wired_source.py -v` — all green.
- **Integration (after Phase 4)**: spin the dev twin per `docs/testing/vault-tests.md`:
  ```bash
  cd /home/mhavranek/git/desktop-connector/desktop
  DC_ALLOW_MULTI_INSTANCE=1 python3 -m src.main \
    --config-dir=~/.config/desktop-connector-dev \
    --server-url=http://127.0.0.1:4441
  ```
  Drive: vault-onboard → bind a small folder → drop in a test file → wait for upload → open Vault Settings → Activity tab → assert "Uploaded   test.txt (this-device)" row visible with the current timestamp. Repeat for delete, restore, grant.
- **AT-SPI snapshot**: `mcp__gtk-a11y__dump_tree` on `vault-main` window, Activity tab — assert at least one row label matches `humanise_event_type(...)` (e.g., "Uploaded").
- **Cross-device**: with B6's harness (deferred — see `docs/plans/skipped-while-autonomous.md:46-57`), verify a second device fetching the manifest sees the entries written by the first.

## Out of scope (followups)

- `archived_op_segments` rotation (separate ciphertext segments + fetch path) — deferred to v1.1 once the in-manifest tail proves stable.
- Per-folder Activity filter in the UI (the schema's `remote_folder_id` would support it once shard tails merge, but the current tab has filename-search only).
- Server-side audit re-introduction (F-S16 retired it; the encrypted op-log is sufficient).
- Webcam QR for `vault-join` (unrelated; tracked in `docs/plans/skipped-while-autonomous.md`).

## Risks

- **R1 — Old-version device strips the tail.** Mitigated by D7: producers always read-modify-write the prior tail rather than constructing fresh. An old-version desktop's `_apply_batch_to_shard` rebuilds candidate from the fetched shard *without* touching `operation_log_tail`, so prior entries survive. New device's appends survive across mixed-version writers. Lowest-risk path.
- **R2 — Tail bloat from chatty ops.** Eviction stages can cascade through dozens of files in one publish. Mitigated by D3's cap; D4 ensures truncation is observable.
- **R3 — Genesis vault has no `vault.create` entry until second revision.** D5 — accepted cost; alternatively a tiny one-shot follow-up publish could land it, but that adds a fragile cross-revision dance for negligible UX gain.
- **R4 — Plan-vs-implementation drift on undecided phases.** Mitigated by phase split: Phase 2 is shippable alone, with a partial timeline (file ops only) — still better than the empty state today.
