# Vault large-folder bind performance

Fix for the cliff B7 surfaced in suite 0004 (2026-05-16): binding a
10 000-file folder takes **2 h 11 min** wall-clock, with per-op rate
decaying from 8.5 → 1.3 ops/s as the encrypted manifest grows. Full
data + cliff analysis: `temp/automation-tests-results/0004/B7-large-folder/result.md`
and §13 of `live-testing-followup.md`.

Two phases. Phase 1 ships the user-visible safety net so nobody waits
two hours wondering if it's broken. Phase 2 removes the cliff so the
warning rarely fires.

---

## Context: where the time goes

`run_backup_only_cycle` in `desktop/src/vault/binding/sync.py` drains
the per-binding pending-ops queue one op at a time. Per op it does:

1. `vault.fetch_manifest(relay)` (sync.py:330 — done after **every**
   prior successful op).
2. Encrypt the file's chunks (~1 chunk for tiny files).
3. PUT chunks (cheap, idempotent dedupe).
4. `vault.publish_manifest(...)` — CAS-publish a manifest revision
   containing N+1 version entries.

Steps 1 and 4 are the cliff: each ships the **full encrypted manifest
envelope** (manifest grows linearly in N), so per-op crypto+I/O cost
grows linearly. For 10k files that's ~50 GB of crypto work to bind a
2.5 MB folder. The architecture is correct; the wins are amortization
opportunities left on the table.

Memory is fine (~210 MiB peak at 10k); CPU is fine (mostly waiting on
AEAD + JSON canonicalize); relay storage is fine (idempotent chunk
dedupe). The only failure mode is wall-clock.

---

## Phase 1 — Pre-bind warning + better progress (the safety net)

**Goal**: a user dropping a 10 000-file folder into a binding should
never wonder if the app is hung. The bind itself stays slow (Phase 2
fixes that), but the user is informed and consents.

### Scope

Single commit. Touches only:
- `desktop/src/vault/binding/preflight.py` (compute file count + size
  + duration estimate)
- `desktop/src/vault_folders_tab.py` (or wherever the Folders-tab
  "Add folder" handler lives) — show the warning dialog before
  enqueueing the bind
- `desktop/src/windows_vault_browser/` for the existing in-progress
  surface — confirm the progress label is good enough or widen it

No protocol change, no manifest change, no server change.

### Wire-up

1. **Preflight count + estimate.** Extend
   `preflight.py:run_preflight` (or add a new
   `estimate_initial_bind_duration` helper) to walk the candidate
   folder once with `os.walk` and return:
   ```python
   @dataclass
   class BindPreflightEstimate:
       file_count: int
       total_bytes: int
       projected_duration_seconds: float
       warning_threshold_hit: bool
   ```
   The walk is the same `_walk_local` shape as
   `vault/binding/scan.py:133-140` — borrow it or call it directly to
   keep semantics identical.

2. **Duration model.** Use the suite 0004 measurements to fit a
   simple model that turns `file_count` + `manifest_size_so_far`
   into a wall-clock estimate. The measured curve is approximately:
   ```
   per_op_seconds ≈ 0.04 + 0.00007 * manifest_entries_after_publish
   ```
   - Empty manifest: ~40 ms/op (matches the 100-file run's ~30 ops/s).
   - 5k entries: ~390 ms/op (matches the 2.6 ops/s checkpoint).
   - 10k entries: ~740 ms/op (matches the 1.3 ops/s checkpoint).
   The integral over `n=0..N` is the total estimate. Round up to
   minutes (or hours) for display.

3. **Warning threshold.** Trigger the dialog when the estimate is
   **≥ 2 minutes** (empirically ~2 000 files on a fresh-vault bind).
   Use the *estimate*, not a raw file count, because the cost also
   depends on the existing manifest size (binding a 1k folder against
   a vault that already has 10k entries is slower than against an
   empty one).

4. **Dialog copy** (proposed):
   ```
   Title: "Large folder — initial sync will take a while"

   Body:
     This folder has <N> files (<size>).
     Encrypting and uploading them will take about <T minutes>.

     During the initial sync your vault is using the desktop's CPU
     and network. You can keep using your computer; the sync runs
     in the background, but the Vault window will say "Syncing X/Y"
     until it finishes.

     <stretch: low-key link to a future "Why is this slow?" doc>

   Buttons:  [Cancel] [Start sync]
   ```
   Use `Adw.MessageDialog` (matches §3.7 rollback banner +
   `fresh_unlock_prompt.py` style).

5. **Progress widening (optional, defer-OK)**: the Vault Browser
   `Syncing X/Y` line already exists. Add an ETA suffix based on the
   measured ops/s so the user can see the curve flatten:
   `"Syncing 4 200 / 10 000 — ~38 min remaining at current rate"`.
   This is one extra label-set in
   `windows_vault_browser/uploads.py`'s progress callback. Optional
   for Phase 1 ship; if it slips, file as a follow-up.

### Acceptance

- Binding a folder with ≤ 1 000 files: no dialog (estimate < 2 min).
- Binding a 5 000-file folder: dialog appears with non-zero estimate,
  Cancel aborts cleanly (no partial binding row, no orphaned remote
  folder), Start sync proceeds as today.
- Estimate displayed matches the actual wall-clock within ~30 %
  on the dev twin against `php -S` (the SO-1 dev-twin starvation
  amplifies cost, so the estimate may be conservative on a real
  multithreaded relay — that's fine).
- One source pin in `tests/protocol/test_desktop_vault_*_source.py`
  for the new estimator + the dialog wiring.

### Status

Open.

---

## Phase 2 — Remove the cliff (the real fix)

**Goal**: a 10 000-file bind takes single-digit minutes, not hours.

Two independent wins. SO-2 is small and ships standalone for a ~2×
gain. SO-3 is the protocol-level amortization for the ~10–50× gain.
Land SO-2 first to validate the measurement harness, then SO-3 on
top.

### SO-2 — Drop the redundant per-op `fetch_manifest`

**Symptom**: per op = **2** manifest round-trips, but only the PUT
mutates state. The GET after each op is wasted bandwidth + decrypt.

**Cause**: `sync.py:328-336` re-fetches the manifest after every
`uploaded|deleted|failed` outcome so the next iteration sees the
latest state. But `publish_manifest` already **returns** the new
manifest dict on success — the loop just isn't reading it.

**Fix shape**:
- `Vault.publish_manifest` returns the published manifest dict.
  Verify (or extend) that contract so the new revision is the same
  bytes the relay accepted.
- In `run_backup_only_cycle`, replace the re-fetch with a `current_manifest = result.manifest` assignment.
- **Keep** the re-fetch on `outcome.status == "failed"` (CAS conflict
  recovery legitimately needs a fresh view).
- Same change in `vault/binding/twoway.py:run_two_way_cycle` if its
  loop has the same shape (audit before assuming).

**Expected speedup**: ~2× on initial bind (half the manifest GETs).
On the suite 0004 10k case: 2 h 11 min → ~65 min. Better, still bad.

**Acceptance**:
- Unit test in `tests/protocol/test_desktop_vault_binding_sync_source.py`
  pinning that `vault.fetch_manifest` is **not** called per
  successful op.
- Live re-test: 1k bind drops from 70 s to ~35 s; 10k bind drops
  proportionally.
- No regression in CAS conflict recovery (concurrent-edit test still
  passes).

**Sizing**: ~5-line code change + 1 test + 1 measurement re-run.
Half a day.

### SO-3 — Batched manifest publish

**Symptom**: every file gets its own manifest revision. For 10k
files = 10k publishes = `O(N²)` bytes of encrypted manifest shipped.

**Cause**: no batching surface in `run_backup_only_cycle`. The loop
encrypt+PUTs one file, publishes, fetches, repeats. Each publish
ships the full encrypted manifest.

**Fix shape — client-only (preferred)**:

The relay's CAS contract doesn't need to change. The client batches
N file-version additions into one manifest mutation, then runs the
existing single CAS publish. Roughly:

```python
PUBLISH_BATCH_SIZE = 50  # tunable
batch_versions = []
current_manifest = head
for op in pending:
    if op.op_type == "upload":
        # Steps 1+2: encrypt + chunk PUTs run as today (idempotent).
        chunks_result = upload_chunks_only(op, vault, relay)
        # NEW: build version entry but don't publish yet.
        batch_versions.append(_make_version_payload(op, chunks_result))
    elif op.op_type == "delete":
        batch_versions.append(_make_tombstone(op))

    if len(batch_versions) >= PUBLISH_BATCH_SIZE or is_last(op):
        # One CAS for the whole batch.
        next_manifest = apply_versions(current_manifest, batch_versions)
        current_manifest = vault.publish_manifest(relay, next_manifest)
        batch_versions.clear()
```

This collapses the 10k-publish run into 200 publishes (`K=50`). The
chunk PUTs still happen per file (they're streamy + idempotent), so
the encrypt-then-publish ordering is preserved.

**CAS conflict handling**: today's path is "single publish; on
conflict, refetch and retry that one op." Batched needs the same
logic at batch granularity — on conflict, refetch, re-apply the
batch's version entries to the new head, retry. The existing
single-op CAS retry helper extends naturally; the conflict-merge for
"add new version of a file" is the trivial case (always wins).

**Edge cases / risks**:
- **Resume after kill**: today, the upload-session JSON
  (`vault/upload/session.py`) tracks per-file chunk progress, but
  *publish* is a single atomic event. With batching, the resume
  banner needs to know "chunks for files 1..K are uploaded but the
  K-way manifest revision was not published yet." Simplest path:
  drop the batch on kill and re-encrypt next time (chunks are
  idempotently dedupe'd by `chunk_id`, so re-uploading them is a
  HEAD-and-skip). The B8 banner already handles the "chunks PUT,
  manifest not published" state for single files — extend that.
- **Two-way conflict path** (`twoway.py`): batching with conflict-
  rename detection is harder, because the conflict detector
  per-file checks the manifest before publishing. Probably keep
  the two-way loop single-publish for now; only the backup-only
  cycle batches. Worth a `--sync-mode` audit before landing.
- **Watcher latency**: today the watcher enqueues one op and the
  drain immediately publishes. Batching adds latency: a single new
  file dropped into the binding waits until either K-1 more files
  arrive or a flush timer fires. **Add a max-batch-age** (e.g.
  500 ms) so single-file edits don't sit. Or batch only during the
  initial-bind drain (state == "needs-baseline") and keep
  steady-state per-file.

**Expected speedup**: at `K=50` with the SO-2 fix already in,
manifest round-trips drop from 2 per op (SO-2 fixed: 1 per op) to
~1 per 50 ops. Combined: **~50× over baseline** for the bind drain.
10k bind: 2 h 11 min → ~3 min.

**Acceptance**:
- New `tests/protocol/test_desktop_vault_binding_batched_publish.py`
  with vectors covering: clean batch, CAS-conflict mid-batch,
  kill-mid-batch resume, mixed upload+delete batch, batch-flush
  on watcher-quiet.
- Live re-test: 10k bind in ≤ 5 minutes against `php -S` (Apache
  multithreaded should be even faster).
- No regression in conflict-rename behaviour in `twoway.py`
  (kept single-publish for now — explicitly out of scope for this
  pass).
- The §A20 conflict-naming invariants (item 8 from
  `live-testing-followup.md`) still hold.

**Sizing**: real engineering work. ~2–3 days for the
backup-only path. Add another day for live re-testing + the
resume banner extension.

### Status

Open. SO-2 first (low risk, validates the measurement loop).
SO-3 lands after, on a separate commit.

---

## What deliberately stays out of Phase 2

- **Server-side batched-publish endpoint**: not needed. The client
  batching uses the existing CAS contract. A future
  `/api/vaults/{id}/manifest/append-versions` could trim
  bandwidth further (ship only the delta, not the whole envelope)
  but that's a v1.x optimization, not the cliff fix.
- **Manifest sharding** (split the manifest into per-folder
  envelopes so each publish only ships the relevant shard): real
  architectural change. Worth its own design doc once the SO-2/SO-3
  baseline is shipped — that'll tell us whether the cliff is
  *truly* gone or just pushed to ~100k files.
- **Two-way conflict path batching**: deferred. The conflict-detect
  loop in `twoway.py` is the harder amortization; the backup-only
  path covers the initial-bind hot case.
- **Watcher / inotify burst-load coverage**: separate test (SO-4
  from §13). Not a performance fix; a coverage gap.

---

## Out-of-scope items recorded for visibility

These are perf nudges discovered while measuring B7 that aren't on
the Phase 1/2 path but should be in the followup queue:

- **Manifest envelope encryption is single-threaded.** AEAD encrypt
  + JSON canonicalize per publish could overlap with the next file's
  chunk encrypt. Marginal at K=50; significant if we ever ship
  per-file publishes for two-way conflicts.
- **Local index `vault-local-entries` lookup** in `scan.py:77-83`
  does one SQLite SELECT per file. For 10k files scan is still
  fast (14.5 s), but a single batched `IN (path1, path2, ...)` query
  would drop scan time by ~5×. Low-priority; not on the cliff.
- **`os.walk` could be replaced by `os.scandir`** in `_walk_local`
  for ~2× fewer syscalls on the walk. Marginal; not the cliff.

---

## Open questions before Phase 1 lands

- **Threshold tuning**: 2 minutes / ~2 000 files is the proposed
  trigger. Should sub-1-minute warnings also fire ("this is going
  to feel slow")? Default is "trigger only on truly painful
  durations." Revisit after live use.
- **Estimator calibration**: the model fit above is from the local
  `php -S` measurement. On a production multithreaded relay the
  constants might shift. Validate against the user's real install
  once Phase 1 is in their hands.
- **Cancel semantics**: if the user clicks Cancel on the warning,
  the binding row should not be created. Confirm
  `vault/binding/lifecycle.py` has a clean unwind path for that
  case (it does for the "Disconnect during bind" path; same shape).
