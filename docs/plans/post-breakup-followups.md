# Post-breakup follow-ups

**Date:** 2026-05-09
**Branch context:** `tresor-vault` (the file-size breakup landed here; this doc
captures what's next).

## Why this doc exists

`desktop-file-size-breakup.md` is done — every one of its eleven items
shipped, plus three follow-up cleanups (`vault_download/` split, dead
re-exports dropped, `vault_local_state` import normalized). Live-testing
items 1–6 in `live-testing-followup.md` shipped on 2026-05-07.

Three open threads remain. Each one is real work, none of them are
documented as standalone plans today:

1. **`vault_*.py` → `vault/` package consolidation.** Mentioned in one
   line at the bottom of breakup item #9, never planned out.
2. **Cross-session orphan vault rows on the relay.** Partial fix landed
   on 2026-05-07; `live-testing-followup.md` §3 spells out the deferred
   options but doesn't pick one.
3. **Live-testing roadmap.** `live-testing-followup.md` invites new
   items but has no list of *what* to drive next.

Pick any one and start; they're independent.

---

## 1. Fold `vault_*.py` into the `vault/` package

### Progress (2026-05-11)

**Wave A — leaf utilities — 8 of 8 done.** Started 2026-05-09,
finished 2026-05-11 on `tresor-vault`:

| Status | Move | Commit |
| --- | --- | --- |
| done | `vault_bytes_format` → `vault/ui/bytes_format.py` | `dad6a9e` |
| done | `vault_time_format` → `vault/ui/time_format.py` | `111bc9b` |
| done | `vault_window_args` → `vault/ui/window_args.py` | `d886cea` |
| done | `vault_atomic` → `vault/atomic.py` | `b3cb3ee` |
| done | `vault_logging` → `vault/diagnostics/logging.py` | `c83c4be` |
| done | `vault_relay_errors` → `vault/relay_errors.py` | `0d5b27d` |
| done | `vault_error_messages` → `vault/error_messages.py` | `8564656` |
| done | `vault_conflict_naming` → `vault/conflict_naming.py` | `b547f2f` |

**Wave B — data primitives — 3 of 3 done.** Finished 2026-05-11
on `tresor-vault`:

| Status | Move | Commit |
| --- | --- | --- |
| done | `vault_passphrase` → `vault/passphrase.py` | `1035b1c` |
| done | `vault_crypto` → `vault/crypto.py` | `9aafbdd` |
| done | `vault_manifest` → `vault/manifest.py` | `a0b4e20` |

Pattern that worked, repeat for Wave C+:

1. `mkdir -p` the new subpackage path + empty `__init__.py` if needed.
2. `git mv` the file to its new home.
3. Rewrite every importer in lockstep using `replace_all=true` per file
   (the substring `vault_<name>` only appears as a module path; no
   collateral matches) — then verify with `grep -rn "vault_<name>"`.
4. **For modules that move INTO `vault/`**: collapse their own
   `from .vault.X import` lines (siblings) to `from .X import`. Wave A
   didn't surface this — its leaf utilities didn't import other vault
   siblings. Wave B's `vault_manifest` had `from .vault.crypto import
   normalize_vault_id` (correct while top-level) which needed to
   become `from .crypto import` after the move; the unit-test failure
   was loud and immediate, but the rewrite is mechanical and worth
   doing in the same commit.
5. Touch matching test source-pins / docstrings that reference the old
   module path.
6. Smoke-test by running the relevant unit-test suite + an import
   round-trip + (for crypto changes) the vault-v1 cross-runtime vectors.
7. One commit per move.

Test files seen so far: `test_desktop_vault_window_args` (source-pin
asserting the dispatcher's import line), `test_desktop_vault_atomic`,
`test_desktop_vault_logging`, `test_desktop_vault_download`,
`test_desktop_vault_upload`. All have runtime imports plus occasional
literal-string assertions; remember to scan tests during each move.

Also watch for string LITERALS that look like module names —
`vault/error_messages.py` has keys like `"vault_manifest_conflict"`
which are server-side error codes, not module paths. Don't sweep
those.

**Waves C–F are entirely todo.** Wave B finished cleanly; the flat
namespace shrank from 44 → 41 modules. Pick up Wave C (subpackages
— binding, folder, grant, migration, import_, export) next.

### Current state

`desktop/src/` carried **52** flat top-level `vault_*.py` modules
alongside three `vault_*/` packages and a tiny `vault/` core package
(7 files, split out by breakup #9). After Wave A + Wave B
(2026-05-09 → 2026-05-11), **41 flat `vault_*.py` modules remain**;
`vault/` now also holds `atomic.py`, `crypto.py`, `passphrase.py`,
`manifest.py`, `relay_errors.py`, `error_messages.py`,
`conflict_naming.py`, `ui/{bytes_format,time_format,window_args}.py`,
and `diagnostics/logging.py`.

```
desktop/src/
  vault/                  # 18 files: original 7 (vault.py, ids,
                          #   canonical, protocols, recovery_kit,
                          #   remote_folders, __init__) plus Wave A's
                          #   atomic, relay_errors, error_messages,
                          #   conflict_naming, ui/, diagnostics/
                          #   plus Wave B's crypto, passphrase, manifest
  vault_upload/           # package (split out by breakup #4)
  vault_download/         # package (split out post-breakup)
  vault_folders/          # package (split out by breakup #6 — Folders TAB UI)
  windows_vault/          # package (split out by breakup #1 — vault windows)
  vault_*.py × 41         # flat
```

The 41 flat files (sorted, hand-grouped):

```
binding subsystem:          vault_bindings.py
                            vault_binding_baseline.py vault_binding_lifecycle.py
                            vault_binding_preflight.py vault_binding_scan.py
                            vault_binding_sync.py vault_binding_twoway.py
                            vault_filesystem_watcher.py
                            vault_runtime.py vault_runtime_watchers.py
folder / remote folder:     vault_folder_actions.py vault_folder_runtime.py
                            vault_folder_ui_state.py vault_connect_folder_dialog.py
grant flow:                 vault_grant.py vault_grant_qr.py vault_grant_wrap.py
                            vault_access_rotation.py
migration:                  vault_migration.py vault_migration_runner.py
                            vault_migration_propagation.py
import / export:            vault_import.py vault_import_runner.py
                            vault_export.py vault_export_reminder.py
data ops:                   vault_restore.py vault_clear.py vault_repair.py
                            vault_integrity.py vault_eviction.py vault_delete.py
                            vault_purge_schedule.py vault_trash.py
local state / index:        vault_local_index.py vault_local_state.py
                            vault_usage.py vault_activity.py
diagnostics:                vault_debug_bundle.py
                            vault_ransomware_detector.py
UI helpers (non-window):    vault_browser_model.py vault_ui_state.py
```

### Why bother

The flat namespace is the symptom of a vault subsystem that's grown into
a nontrivial chunk of the codebase. Concrete pain:

- **Discoverability.** New contributor reading `desktop/src/` sees
  `vault_atomic.py` next to `vault_repair.py` next to
  `vault_window_args.py` and has no signal about which is plumbing,
  which is data ops, which is UI glue.
- **Cohesion lost.** The eight `vault_binding_*.py` files form a
  state machine that nobody can see at a glance because the
  filesystem doesn't show them as a group.
- **Import hygiene.** Anything that imports "the vault" today picks
  pieces from the flat layer and from the package layer — both styles
  coexist.
- **Future tab moves.** Whenever we extract a UI tab into its own
  subprocess (which we keep doing), it has to import from this flat
  scatter; the package layout would let it depend on `vault.binding`
  instead of seven sibling files.

### Target layout

```
desktop/src/vault/
  __init__.py                       # public façade — re-exports the
                                    # current top-level vault_*.py names
                                    # for one release window
  core/                             # already exists in spirit; promote it
    __init__.py
    vault.py protocols.py ids.py canonical.py
    recovery_kit.py remote_folders.py
  crypto.py passphrase.py
  manifest.py atomic.py
  binding/
    __init__.py
    bindings.py baseline.py lifecycle.py preflight.py scan.py
    sync.py twoway.py filesystem_watcher.py runtime.py runtime_watchers.py
  folder/
    __init__.py
    actions.py runtime.py ui_state.py connect_dialog.py
  grant/
    __init__.py
    grant.py qr.py wrap.py access_rotation.py
  migration/
    __init__.py
    migration.py runner.py propagation.py
  import_/                          # `import` is reserved
    __init__.py
    import.py runner.py
  export/
    __init__.py
    export.py reminder.py
  upload/                           # MOVE existing top-level vault_upload/
  download/                         # MOVE existing top-level vault_download/
  ops/
    __init__.py
    restore.py clear.py repair.py integrity.py eviction.py
    delete.py purge_schedule.py trash.py
  state/
    __init__.py
    local_index.py local_state.py usage.py activity.py
  diagnostics/
    __init__.py
    logging.py debug_bundle.py ransomware_detector.py
  relay_errors.py                   # typed exception classes
  error_messages.py                 # humanize() translation table
  conflict_naming.py                # §A20 conflict-rename helper
                                    # (the breakup principle "Move first,
                                    # refactor second" wins over the
                                    # earlier "merge into one errors.py"
                                    # idea — these three are distinct
                                    # concerns, co-locate them in vault/
                                    # first, merge later if it still
                                    # feels right)
  ui/
    __init__.py
    browser_model.py ui_state.py window_args.py
    time_format.py bytes_format.py
```

Naming notes:
- **`vault/folders/` collision.** Top-level `vault_folders/` is the
  *Folders tab UI* (a GTK4 widget tree). Inside the new `vault/`
  package, the data-layer name `vault.folder` is singular to keep it
  clearly different from the GTK tab. The Folders TAB itself does not
  move into `vault/` — it stays under `windows_vault/folders_tab.py`
  (or sibling to `windows_vault/`) because it's UI, not vault logic.
  Track the rename `vault_folders/` → `windows_vault/folders_tab/`
  in the same wave as the data-layer move so importers fix in one
  pass.
- **`import_` trailing underscore.** Python's `import` keyword
  forbids the bare name; `import_` is the established convention.
- **No `vault.errors` consolidation in Wave A.** Original plan
  proposed merging `error_messages` + `relay_errors` + `conflict_naming`
  into one file. On execution this turned out to overreach: the three
  cover distinct concerns (typed exceptions / user-facing translations /
  conflict-rename utility), and the breakup principle "Move first,
  refactor second" applies to merges too. They each move into `vault/`
  as separate top-level modules; a later commit can merge if it still
  feels right.

### Sequencing

Don't do this in one PR. The import graph touches 100+ files
(`grep -rln "from \.vault_" desktop/src/` returns 103 hits). Move-only
discipline (breakup principle #1) keeps each wave reviewable.

**Wave A — leaf utilities.** No vault module imports them; they import
nothing from vault. Move first to validate the rhythm.

```
vault/ui/bytes_format.py     ← vault_bytes_format.py
vault/ui/time_format.py      ← vault_time_format.py
vault/ui/window_args.py      ← vault_window_args.py
vault/relay_errors.py        ← vault_relay_errors.py
vault/error_messages.py      ← vault_error_messages.py
vault/conflict_naming.py     ← vault_conflict_naming.py
vault/atomic.py              ← vault_atomic.py
vault/diagnostics/logging.py ← vault_logging.py
```

Each move:
1. Add new file.
2. Top-level `vault_<name>.py` becomes a one-line shim:
   `from .vault.ui.bytes_format import *  # noqa`.
3. New imports use the new path; old imports keep working.
4. After all callers migrate (separate PR per area), delete shims.

**Wave B — data primitives.** Crypto, passphrase, manifest. Same
pattern. These have a small fan-out (3–10 importers each) so they
flush quickly.

**Wave C — subpackages, one per PR.** `binding/`, `folder/`, `grant/`,
`migration/`, `import_/`, `export/`. Each subpackage groups 3+ files
that already belong together. Internal imports inside the subpackage
become relative (`from .baseline import ...`) which is the diff
sweet-spot — most lines change in one folder.

**Wave D — operations.** `ops/`, `state/`, `diagnostics/`. Largest
fan-out, do last when the import graph has already stabilized.

**Wave E — promote `vault_upload/` and `vault_download/` into
`vault/upload/` / `vault/download/`.** These are already packages so
the move is `git mv` + import-path rewrite; no internal restructure.

**Wave F — delete shims.** Once every caller imports from the new
path, sweep `desktop/src/vault_*.py` and remove the one-line shims.
Verify with `grep -rln "from src\.vault_" desktop/ tests/` empty.

Each wave: one PR, one `./test_loop.sh` pass, one vault-tests.md
walk-through. Behavior-byte-identical — failing tests mean the move
broke something and must be backed out, not "fixed forward".

### Alternatives considered

- **Single big-bang PR.** Rejected: the file-size breakup taught us
  that incremental, mechanical, byte-for-byte moves are reviewable
  while a single 100-file rename is not.
- **Keep flat, sort alphabetically, accept it.** Rejected: 52 files
  is past the threshold where the filesystem stops being a useful
  index. The eight `vault_binding_*` files alone benefit from
  package grouping more than from any naming convention.
- **Move only the obvious clusters** (binding, grant, migration) and
  leave the rest flat. Tempting but creates a worse layout than
  either extreme — half the vault subsystem in `vault/`, the other
  half in flat siblings, two equally-valid places for new code.

### Acceptance

- `ls desktop/src/vault_*.py` returns nothing.
- `desktop/src/vault/` is the only place vault Python lives.
- `./test_loop.sh` green on every wave.
- `docs/testing/vault-tests.md` 9-test suite passes against the dev
  twin on the final wave.
- One PR per wave; each PR's diff is mostly `git mv` and import
  rewrites, no logic changes. (The breakup-plan principle: behavior
  changes belong in a separate commit.)

### Notes for whoever picks this up

- Keep `desktop/src/vault/__init__.py` thin. Its only job during the
  transition is the re-export shim list. After Wave F, it can become
  a proper package façade exporting just the public API.
- AppImage builds: the Pyinstaller-style packaging in
  `desktop/packaging/appimage/` walks `desktop/src/` recursively, so
  no recipe change should be needed. Verify on Wave A by building an
  AppImage and confirming the packaged tree.
- Architecture decision: this rename is structural-only and doesn't
  cross any threat-model or protocol boundary, so it does **not**
  need a `docs/architecture-decisions.md` entry per project policy
  (decisions log is for non-trivial choices about protocol / state
  machine / security boundary / dependency / threat-model
  assumptions). A brief mention in the commit messages is enough.

---

## 2. Cross-session orphan vault rows

### Where this stands today

Documented in detail at `docs/plans/live-testing-followup.md` §3
(lines 112–155), marked **Status: partial**. The 2026-05-07 fix
collapsed the in-session window between "publish_initial" and
"config.save" to microseconds. Cross-session orphans — rows
published in a wizard session that was abandoned before
`config.save` ran — still leak.

### Pick one of two paths

The deferred options spelled out in `live-testing-followup.md` §3
remain the two viable shapes. We need to commit to one.

**Path A — Resume affordance (no server changes).** When the wizard
opens and finds `state["vault"]["has_pending_publish"] == True` plus
a stored vault id from a prior session, show a one-screen "Resume
previous attempt" panel. User chooses Resume (re-use the pending
material, re-publish if needed, finish) or Discard (clear local
pending state — orphan stays on relay forever, gets GC'd by retention
policy or stays as harmless ciphertext nobody can decrypt).

Pros:
- Zero server change; zero protocol change.
- Reuses the existing idempotent `_pending_publish` machinery.
- Honest UX — surfaces the prior attempt instead of silently
  forgetting it.

Cons:
- Orphans accumulate forever absent retention. Server operator
  has no signal about which rows are abandoned vs deliberate.
- "Resume" only works on the same desktop where the original
  attempt happened.

**Path B — Scoped DELETE endpoint.** Add `DELETE /api/vaults/{vault_id}`
authenticated by the device id that authored the vault. Wizard fires
DELETE on cancel, on next-session retry-with-discard, and on close
during the recovery-kit panel.

Pros:
- Clean. No orphans on relay.
- Server operator's vaults table reflects reality.

Cons:
- Server surface change — needs careful spec, threat model
  (what stops a paired peer from deleting?), and migration entry.
- Relay schema/auth bookkeeping for "device that authored this
  vault id" — currently the relay only knows about vault headers
  and grants, not authorship.
- Adds an `architecture-decisions.md` entry's worth of design
  surface to the threat model.

**Recommended:** Path A, ship it on `tresor-vault`. Path B is the
right long-term answer if/when we need server-side cleanup for any
other reason (export-bundle GC, expired grants, etc.); doing it for
this case alone is overkill.

### Acceptance (Path A)

- Onboarding wizard, on launch, detects `has_pending_publish` from a
  prior session and shows the Resume panel before the passphrase
  step.
- Resume completes the original publish (no new vault id generated)
  and walks to the success screen.
- Discard wipes local pending state with the existing
  `feedback_security_ux` warning + confirmation gate (per the
  user's standing rule on security-material flows).
- Single onboarding session leaves at most one `vaults` row on the
  relay regardless of how many times the wizard was opened and
  closed.
- Test pin in `tests/protocol/test_desktop_vault_*.py` covers the
  Resume vs Discard branches at the worker-thread layer.

---

## 3. Live-testing roadmap

### Where this stands today

`docs/plans/live-testing-followup.md` invites new items as they
surface; `docs/testing/vault-tests.md` is the harness (9-test
chained suite against an isolated dev twin). What's missing is a
list of *which flows haven't been driven yet* — without it, "do
more live testing" is open-ended.

### Flows already exercised (per `vault-tests.md` 9-test suite)

1. Onboard from scratch.
2. Add remote folder + sync.
3. Add binding to local folder + sync up.
4. Modify local file + sync up.
5. Sync down change from another device.
6. Switch passphrase.
7. Disconnect / reconnect device.
8. Export / re-import bundle.
9. Recovery from kit.

### Flows worth driving live next

These are not in the chained suite. Each is one focused live-test
session against the dev twin; surface bugs into
`live-testing-followup.md` items 7+.

- **Eviction under quota pressure.** Fill the relay quota, observe
  the desktop's eviction pass during upload, verify the right
  versions get culled (and that "Show deleted" surfaces them).
- **Resume upload after kill.** Start a multi-GB upload, kill the
  desktop subprocess mid-chunk, restart, verify the resume banner
  fires and the upload completes without re-uploading already-stored
  chunks. Cancel button on the resume banner (the 2026-05-06 fix —
  commit `2810201`) should also be exercised.
- **Cross-device grant + accept on a fresh device.** Exercise the
  QR-grant join flow end-to-end on the dev twin's secondary device.
- **Concurrent edits with binding sync.** Edit the same file on
  both devices between syncs; verify the conflict-rename path
  (`vault_conflict_naming.py`) produces predictable output and the
  Activity tab logs both branches.
- **Large folder bind.** Attach a folder with 10k+ small files;
  verify baseline scan completes, sync up doesn't OOM, manifest
  publishes successfully. Watch for `vault_binding_scan.py` /
  `vault_filesystem_watcher.py` performance cliffs.
- **Migration switch-back.** Migrate from one relay to another,
  then switch back, verify both sides agree on manifest revision.
- **Ransomware detector trip.** Simulate a mass-rewrite event in a
  bound folder; verify `vault_ransomware_detector.py` pauses sync
  and surfaces the warning.
- **Wrong-passphrase rate-limit.** Verify the keyring-backed retry
  budget and the human-readable error path.
- **Schedule purge.** Set a purge schedule, fast-forward time
  (mock `_now_rfc3339` if needed), verify the scheduled purge
  fires and audits correctly.
- **Debug bundle on a real install.** Generate a bundle, inspect
  the contents, confirm no plaintext / no keys / no tokens leak
  per the logging policy in CLAUDE.md.

### How to capture findings

Per `live-testing-followup.md`'s closing line: append new items
below the existing six, keeping the same shape (Symptom / Cause /
Fix shape / Acceptance / Status). Items 1–6 are the template.

### Acceptance for "this thread is done"

Not really — live testing is continuous. A reasonable milestone is
"every flow on the list above has at least one live-test session
recorded in `live-testing-followup.md` items 7+", which would
suggest the suite in `vault-tests.md` should be extended to chain
the high-value ones (eviction, resume-after-kill, cross-device
grant) so future sessions can run them automatically.

---

## Sequencing recommendation

If picking up cold:

1. **Item 2 first** (cross-session orphans, Path A). Smallest, ships
   one focused commit on `tresor-vault`, immediately improves the
   onboarding flow.
2. **Item 3** (live-testing flows). Pick two or three from the list
   above and run them; capture findings as items 7+ in
   `live-testing-followup.md`.
3. **Item 1** (vault package consolidation). Big. Wave A and Wave B
   are mechanical and safe; Wave C onward needs concentrated
   review windows. Don't start Wave C the day before a release tag.

These don't block each other — feel free to interleave or tackle
out of order.
