# Vault automation tests — interactive guide

This is a guide **Claude follows interactively** when the user types
"run test N". Not a script. Each test step lists the exact command,
the AT-SPI action, the assertion, and the artefacts to capture.

The user's real Desktop Connector install + real vault are **never
touched**. The guide spins up an isolated dev twin (separate
config dir, separate keypair, separate vault id) talking to a local
PHP relay, runs the test, writes results, and tears down on the last
test of the suite.

---

## Setup (run once at start of suite)

The dev twin coexists with the user's real install via three knobs:
isolated `--config-dir`, isolated `--server-url`, and the
`DC_ALLOW_MULTI_INSTANCE=1` env var that bypasses
`enforce_single_instance` (`desktop/src/bootstrap/appimage_relocate.py`).

Three shared-resource auto-isolation guarantees back the dev twin up
so a forgotten env var or wrong invocation can't reach into the user's
real state:

- **Keyring service** is derived from ``config_dir.name``
  (``Config.__init__``) — see the 2026-05-06 entry below.
- **File-manager XDG scripts** carry a ``# CONFIG: <name>`` marker so
  the dev twin can't delete the canonical install's
  ``~/.local/share/nautilus/scripts/Send to <peer>`` entry on startup
  (root cause of suite 0002 test-02; see
  ``docs/architecture-decisions.md`` 2026-05-06).
- **Single-instance enforcement** is opt-out via
  ``DC_ALLOW_MULTI_INSTANCE``; without that env var, two DC processes
  would SIGTERM each other.

### Paths

| What | Path |
|---|---|
| Dev config dir | `~/.config/desktop-connector-dev/` |
| Dev server data | `server/data/connector.db` (project-relative) |
| Dev server logs | `server/data/logs/server.log` |
| Dev relay URL | `http://127.0.0.1:4441` |
| Suite results | `temp/automation-tests-results/<NNNN>/test-NN/` |

`<NNNN>` is the suite-run sequence (`0001`, `0002`, …). Pick the next
free integer at suite start (`ls temp/automation-tests-results/`).

### Start the local PHP relay (suite-start)

```bash
cd /home/mhavranek/git/desktop-connector
# Wipe any prior dev server state for a clean suite
rm -f server/data/connector.db
rm -rf server/storage/* server/data/logs
mkdir -p server/data/logs server/storage
# Run in background
php -S 127.0.0.1:4441 -t server/public/ \
    > server/data/logs/php-server.stdout.log \
    2> server/data/logs/php-server.stderr.log &
echo $! > /tmp/dc-test-php.pid
```

### Wipe the dev config dir (suite-start)

```bash
rm -rf ~/.config/desktop-connector-dev
```

### Common dev-instance command (used by tests 2+)

```bash
DC_ALLOW_MULTI_INSTANCE=1 \
PYTHONPATH=/home/mhavranek/git/desktop-connector/desktop \
python3 -m src.main \
    --headless \
    --no-pair \
    --config-dir=/home/mhavranek/.config/desktop-connector-dev \
    --server-url=http://127.0.0.1:4441 \
    -v
```

`cwd` for this command should be `/tmp` (not the project tree) so
**neither** instance matches the other's single-instance scan in the
hypothetical case the env var is forgotten.

Two isolation knobs and one auto-isolation guarantee:
- `DC_ALLOW_MULTI_INSTANCE=1` — bypass `enforce_single_instance` so
  the dev twin doesn't SIGTERM the user's real install (and vice
  versa).
- `--no-pair` — skip the pairing-poll loop. Vault is account-less;
  vault tests don't need a paired phone.
- **Keyring isolation is automatic.** ``Config`` derives the keyring
  service name from ``config_dir.name`` (here ``desktop-connector-dev``),
  so the dev twin's ``private_key:pem`` and ``auth_token`` write to
  service ``desktop-connector-dev`` rather than aliasing the user's
  real install at service ``desktop-connector``. No env var needed.
  ``DC_KEYRING_SERVICE`` still works as a global override for
  unusual setups, but no test should set it — leave the auto path
  alone.

Why this matters: an earlier version of this guide required
``DC_KEYRING_SERVICE=desktop-connector-dev``. The first run of the
suite forgot to set it, the dev twin loaded the user's real X25519
key from the keyring and overwrote the user's real ``auth_token``
with the local relay's value, and the user's real install lost
auth with their real server. After re-pairing, the auto-derivation
landed in ``Config.__init__`` so the same mistake cannot happen
again — even a typo'd env var or forgotten override stays
isolated.

### Common GTK4 window command (vault windows)

```bash
DC_KEYRING_SERVICE=desktop-connector-dev \
PYTHONPATH=/home/mhavranek/git/desktop-connector/desktop \
python3 -m src.windows <kind> \
    --config-dir=/home/mhavranek/.config/desktop-connector-dev
```

Where `<kind>` is one of `vault-main`, `vault-onboard`, `vault-browser`,
`vault-import`, `vault-passphrase-generator`. The window is its own
process; `enforce_single_instance` doesn't fire on windows (different
cmdline). No env-var bypass strictly needed for windows, but the
keyring-service override is still required so the window doesn't
read the user's real device key.

### Vault UI tests run **without** the headless dev twin

`php -S` is single-threaded. The headless dev twin's poller sits in a
25-second long-poll on `/api/transfers/notify`; while it's parked,
**every** other request — including the vault UI's manifest GET / PUT
— queues behind it. A live test run hit a ~50-second wait on a
single "Add folder" click because the GET landed at the 25 s timeout
and the PUT at +25 s after that.

Vault is account-less and the receiver only matters for transfer
tests. **Never run the headless dev twin during a vault UI test on the
same `php -S` relay.** If a future test needs both halves running,
spawn a second PHP worker on a different port (e.g. `4442`) for the
receiver and leave `:4441` free for UI traffic.

### ⛔ HARD RULE: never set `GTK_A11Y=atspi`

**Do not, under any circumstances, set the `GTK_A11Y=atspi`
environment variable on any test invocation on this machine.**

On 2026-05-06, launching `python3 -m src.windows vault-main` with
`GTK_A11Y=atspi env=…` crashed the user's GNOME Wayland session and
logged them out. The window subprocess wrote one log line —
`Gdk-Message: Error reading events from display: Broken pipe` — and
the entire session went away.

The gtk-a11y MCP's own `mcp__gtk-a11y__status` docstring already
warns about flipping the gsetting on Wayland; the env var path is
just as dangerous on this host's GNOME / Wayland combo.

AT-SPI is already enabled system-wide on this machine
(`python3-dogtail`, `accerciser`, `at-spi2-core` per CLAUDE.md).
GTK4/libadwaita windows publish to the bus without any env override.

If after launching a window `mcp__gtk-a11y__list_apps` doesn't show
it, **do not "fix" it by adding `GTK_A11Y=atspi`**. Investigate via
accerciser or fall back to non-AT-SPI assertions
(process alive + log content + DB state).

### Teardown (run at end of suite or on user request)

```bash
pkill -f "src.main.*desktop-connector-dev" 2>/dev/null
pkill -f "src.windows.*desktop-connector-dev" 2>/dev/null
# Stop PHP server
[ -f /tmp/dc-test-php.pid ] && kill "$(cat /tmp/dc-test-php.pid)" && rm /tmp/dc-test-php.pid
```

The dev config dir + dev server DB are **left in place** after teardown
so the user can inspect post-mortem. The next suite starts by wiping.

### Per-test artefact directory

Before each test, create:

```bash
SUITE=0001  # set once per suite
TEST=test-01  # set per test
mkdir -p temp/automation-tests-results/$SUITE/$TEST/screenshots
```

The result file is `result.md` in that dir. Screenshots go in the
`screenshots/` subfolder, named `NN-<short>.png` in capture order.

### Result.md template (per test)

```markdown
# Test NN — <name>

- **Suite**: 0001
- **Started**: 2026-05-06T14:33:12Z
- **Result**: PASS | FAIL | BLOCKED
- **Branch**: tresor-vault @ <commit>

## Steps observed
1. …
2. …

## Assertions
- [x] <assertion>
- [ ] <assertion>

## Artefacts
- screenshots/01-foo.png — caption
- relevant log excerpts inline (NEVER paste secrets/keys/tokens)

## Notes
<anything surprising; suggested follow-up>
```

### Logging discipline

Per CLAUDE.md `docs/diagnostics.events.md`: never paste keys, auth
tokens, FCM tokens, decrypted clipboard content, or encrypted
payloads into result.md. Paste error messages, event tags, and
`device_id`/`vault_id` (already opaque ids).

### What "PASS" means

A test passes only if **all** its assertions hold AND the dev server
log shows no unexpected `apierror.caught` entries between the test's
start and end timestamps. A side-effect-free assertion is not enough
— if a test claims "vault unlocks", the dev server must show the
expected `vault.*` events too.

---

## Test 01 — Local relay is up and advertises vault_v1

**Goal**: confirm the harness's foundation. Without this, every other
test is meaningless.

**Preconditions**: PHP server started per suite-start.

**Steps**:
1. `curl -s http://127.0.0.1:4441/api/health` → JSON.
2. Save the raw response to `result.md` (it's harmless metadata).

**Assertions**:
- HTTP 200.
- `capabilities` array contains `"vault_v1"`.
- `capabilities` array contains `"stream_v1"` (sanity: the relay isn't
  serving a stripped build).

**Capture**: response JSON inline in `result.md`. No screenshot
(headless test).

---

## Test 02 — Dev desktop instance launches, registers, no errors

**Goal**: the dev twin can boot in isolation against the local relay
and complete device registration.

**Preconditions**: Test 01 PASS. Dev config dir wiped per suite-start.

**Steps**:
1. Launch the common dev-instance command in the background. Tee
   stdout+stderr into the test artefact dir
   (`dev-instance.{stdout,stderr}.log`).
2. Sleep 3 s.
3. Inspect the relay-side device row:
   `sqlite3 server/data/connector.db 'SELECT count(*), device_type, substr(device_id,1,12), last_seen_at FROM devices;'`
   (column is `device_id`, not `id`; the table has no `id` column).
4. The captured `dev-instance.stderr.log` IS the desktop log for this
   test. The opt-in `~/.config/desktop-connector-dev/logs/desktop-connector.log`
   does **not** exist on a fresh wipe (logging is gated on the Settings
   "Allow logging" toggle per CLAUDE.md "Logging (opt-in)"); don't
   tail it.

**Assertions**:
- Process is still alive after 3 s (not crashed).
- Exactly one row in `devices` table, `device_type=desktop`.
- No `ERROR`/`CRITICAL` lines in `dev-instance.stderr.log`.
- `~/.config/desktop-connector-dev/config.json` exists and contains a
  `device_id` field.
- Stderr contains
  `config.secrets.using_keyring service=desktop-connector-dev`
  (proof the per-config keyring derivation kicked in).
- Stderr contains **no** `file_manager.*.cleaned` or
  `file_manager.*.legacy_removed` lines (proof the dev twin didn't
  reach into the canonical install's XDG scripts dir; if either fires,
  stop the suite — that's the bug from 2026-05-06).

**Capture**: stderr tail inline (with secrets scrubbed — keep only
event tags). No screenshot.

**Leaves running**: the headless dev instance stays up for tests 7+.
Tests 3–6 stop it explicitly because they only need the windows.

---

## Test 03 — Vault main window opens against an empty config

**Goal**: confirm the GTK4 vault entrypoint renders and exposes
itself over AT-SPI.

**Preconditions**: Test 02 PASS. Stop the headless dev instance for
this test (`pkill -f "src.main.*desktop-connector-dev"`) — we only
need the window.

**Steps**:
1. Launch the common GTK4 window command with `<kind>=vault-main`.
   Background; capture stdout+stderr.
2. Sleep 1.5 s for the window to render.
3. `mcp__gtk-a11y__list_apps` — confirm a Desktop Connector window is
   visible.
4. `mcp__gtk-a11y__dump_tree` for that app — save as
   `screenshots/02-attree.txt`.
5. Capture an on-disk screenshot via
   `gnome-screenshot -w -f screenshots/01-vault-main-empty.png`. (The
   `mcp__gtk-a11y__screenshot` tool returns the image inline only — no
   file is saved automatically.)
6. Close the window: `pkill -f "src.windows vault-main.*desktop-connector-dev"`.
   (The `kill $(cat …pid)` form orphans the python child because
   `echo $!` after a backgrounded `python3 -m src.windows …` captures
   the bash wrapper PID, not the python child PID. Always use `pkill
   -f` for window teardown.)

**Assertions**:
- A window with title containing "Vault" is in the AT-SPI app list.
- The dump tree contains the string "(no vault opened)" or "(no
  vault)" — i.e. the empty-state copy at `windows_vault.py:93`.
- Window stderr is empty (no GTK warnings, no Python tracebacks).
- The body uses a **left vertical sidebar** (`Gtk.StackSidebar` over
  `Gtk.Stack`), not a top tab strip. The dump tree shows ten
  side-by-side `panel '<title>'` entries — Recovery, Folders, Devices,
  Security, Sync safety, Storage, Activity, Maintenance, Migration,
  Danger zone — each backed by a `Gtk.Stack` page. Recovery is the
  default page; the others are reachable via the sidebar.
- Devices, Security, Sync safety, Storage are **deliberate
  placeholders**. Activating each shows a `title-3` heading with the
  tab name plus a dim-label line "This panel is reserved for later
  development. No controls are available yet." The harness must not
  treat their lack of interactive widgets as a failure.

**Capture**: window screenshot + AT-SPI tree dump.

---

## Test 04 — Vault onboard wizard creates a vault end-to-end

**Goal**: the create-vault happy path. After this test, the dev relay
holds one vault and the dev config knows it.

**Preconditions**: Test 03 PASS. No vault exists in dev relay
(`SELECT count(*) FROM vaults` → 0).

**Steps**:
1. Launch the GTK4 window command with `<kind>=vault-onboard`.
2. Sleep 1.5 s. Screenshot `01-onboard-step1-choose.png`.
3. AT-SPI: dump tree, find the "Create new vault" path — click it
   (`mcp__gtk-a11y__click_by_path` on the matching button).
4. Sleep 0.5 s. Screenshot `02-onboard-step2-passphrase.png`.
5. AT-SPI: locate the recovery-passphrase entry (label "Recovery
   passphrase" or accessible name set at `windows_vault.py:336`) and
   the confirm entry. Type a known fixed test passphrase (record
   only its **length and entropy class**, not the value, in
   result.md — but the value itself can be a fixed string like
   `correct horse battery staple seven words` since it's an
   automation fixture; mention this in Notes).
6. Click the "Continue" / "Create" button (look it up in dump tree
   first — exact label may vary).
7. Wait up to 10 s for either the success step or an error label.
   Poll the dump tree every 1 s for a node containing "success" /
   "Vault created" / "✓".
8. Screenshot `03-onboard-success.png`.
9. Close the window.

**Assertions**:
- Wizard advanced from `choose_path` → `create_passphrase` →
  `success`. Verify by widget-tree mutation across the three steps:
    - Step 1 has `push button 'Create a new vault'` and `push button 'Import from export…'`.
    - Step 2 has `password text 'Recovery passphrase'`, `password text 'Confirm passphrase'`, and `push button 'Continue'`.
    - Step 3 has `label 'Vault created'`, `label 'Your Vault ID:'`, the dashed `XXXX-XXXX-XXXX` text widget, and `push button 'Export and verify recovery kit…'`.
  Use `mcp__gtk-a11y__wait_for_widget` to poll the tree for the `'Vault created'` label (≤ 15 s).
- `sqlite3 server/data/connector.db 'SELECT count(*), substr(vault_id,1,12), header_revision FROM vaults;'`
  → exactly **1** row, `header_revision=1`, with the same 12-char id
  the wizard displayed (with dashes stripped).
- `~/.config/desktop-connector-dev/config.json` carries the new vault
  id at `vault.last_known_id` (matches the DB id) and has
  `vault.recovery_envelope_meta` populated.
- **Do not** assert on a `vault.create.*` event in
  `server/data/logs/server.log` — the relay never emits one
  (`grep -rn "vault\\." server/src/Controllers/Vault*.php` shows
  only `vault.gc.unlink_failed`). The DB-row + config-id assertions
  above are the proof that the publish was accepted.

**Preconditions** for the relay-publish step:
- Device must be **fully** registered before launching the wizard:
  `~/.config/desktop-connector-dev/config.json` has a `device_id` (or
  the secret store does — post-2026-05-06 fix moves it to keyring),
  AND keyring service `desktop-connector-dev` has a matching
  `auth_token`. If either is missing, the wizard reaches the local
  "Vault created" step but the relay rejects the publish with
  *"Desktop Connector is not registered with the relay"* and a
  "Retry publish" button. To put the harness into that good state,
  start the headless dev twin briefly (test 02's command) so
  registration completes; verify with
  `python3 -c "import json; print('device_id' in json.load(open('/home/mhavranek/.config/desktop-connector-dev/config.json')))"`
  or by checking the keyring entry. Stop the dev twin before
  launching the wizard (the wizard owns the relay session for this
  test).

**Capture**: 3 screenshots + AT-SPI tree dump of the success step +
DB row count before/after.

---

## Test 05 — Vault main window now reports the vault as connected

**Goal**: state from test 04 is observable in the main window;
nothing is "wizard-only".

**Preconditions**: Test 04 PASS. Vault exists.

**Steps**:
1. Launch `<kind>=vault-main`.
2. Sleep 1.5 s. Screenshot `01-vault-main-connected.png`.
3. Dump tree → save as `02-attree.txt`.
4. Close window.

**Assertions**:
- The window does **not** show the "(no vault opened)" empty-state
  string from test 03.
- A vault id (formatted like `XXXX-XXXX-XXXX`, see vault wordlist
  format) is visible somewhere in the tree.
- The full sidebar — Recovery, Folders, Devices, Security, Sync
  safety, Storage, Activity, Maintenance, Migration, Danger zone —
  is present in the AT-SPI tree, exactly as in test 03 (the layout
  is the same `Gtk.StackSidebar` regardless of vault state).

**Capture**: screenshot + AT-SPI tree.

---

## Test 06 — Lock + relock cycle through the headless runtime

**Goal**: the AEAD header round-trips. Locking forces re-derivation
on next unlock.

**Preconditions**: Test 05 PASS.

**Background — actual unlock model.** The vault is account-less and
the relay only ever sees ciphertext. The desktop derives the master
key with Argon2id from the recovery passphrase **once** during
create / import (test 04), wraps it as a `VaultGrant`, and caches the
grant in the OS keyring (production: libsecret) under the
config-derived service name. From that point on the desktop opens the
vault transparently by loading the grant from keyring — there is no
daily passphrase prompt and no `vault.lock.*` / `vault.unlock.*` /
`vault.kdf.argon2id` events to assert on. "Lock" in this codebase
means **deleting the grant**; "unlock" only happens on a fresh device
via the import wizard. Earlier drafts of test 06 modeled a daily
unlock flow that doesn't match the real design — see
`docs/architecture-decisions.md` 2026-05-06 entries.

**Steps**:
1. Start the headless dev instance (from test 02). Sleep 2 s.
2. Confirm the boot stderr shows
   `vault_grant.backend.keyring service=desktop-connector-dev`
   (not the canonical `desktop-connector` — that's the isolation
   bug from suite 0002 test 06).
3. Verify the runtime sees the vault id from test 04 by reading
   `~/.config/desktop-connector-dev/config.json` (`vault.last_known_id`)
   and confirming it matches the keyring entry.
4. Stop the dev instance (`pkill -f "src.main.*desktop-connector-dev"`).
5. Restart the dev instance with the same args. Vault grant must
   still be present (keyring is durable across kills) so the new
   process opens the vault transparently — no passphrase prompt.
6. Confirm the second boot's stderr again shows
   `vault_grant.backend.keyring service=desktop-connector-dev`.

**Assertions**:
- Both boots emit `vault_grant.backend.keyring service=desktop-connector-dev`
  on stderr — proof the per-config service derivation kicks in (and
  proof the dev twin isn't reading from the canonical user's
  keyring).
- `python3 -c "import keyring; print(keyring.get_password('desktop-connector-dev', 'vault_grant:QRJCRIE7AXEU') is not None)"`
  returns `True` after the first boot AND survives the kill+restart.
- `python3 -c "import keyring; print(keyring.get_password('desktop-connector', 'vault_grant:QRJCRIE7AXEU'))"`
  returns `None` (canonical user's keyring must not hold the dev's
  grant — that's the isolation invariant).
- No `ERROR` / `CRITICAL` lines on dev-instance stderr.
- No passphrase prompt on either boot (the grant cache is the
  daily-use unlock path).

**Capture**: stderr excerpt with the `vault_grant.backend.keyring`
line + keyring probe output.

---

## Test 07 — Bind a folder to the vault

**Goal**: first state-creating action against an unlocked vault.

**Preconditions**: Test 06 PASS. Restart the headless dev instance
(test 06 step 1) so the keyring grant is loaded into the runtime;
no passphrase prompt expected.

**Steps**:
1. Create a temp source folder:
   `mkdir -p /tmp/dc-vault-test-A && touch /tmp/dc-vault-test-A/.keep`.
2. Open `vault-main`. Switch to the **Folders** tab (built by
   `vault_folders_tab.py` per `windows_vault.py:512`).
3. Screenshot `01-folders-tab-empty.png`.
4. Drive the "Add folder" / "Bind folder" action via AT-SPI. The
   exact label needs to come from the dump tree at runtime.
5. Pick `/tmp/dc-vault-test-A`. Confirm.
6. Wait up to 10 s for the binding to land. Poll the tree for the
   path string.
7. Screenshot `02-folders-tab-bound.png`.

**Assertions**:
- The Folders tab now lists `/tmp/dc-vault-test-A`.
- Local index DB at
  `~/.config/desktop-connector-dev/vault-local-index.sqlite3`
  contains a row for the binding.
- The dev server log shows a `vault.binding.*` event for this
  vault id.

**Capture**: 2 screenshots + tree dump.

---

## Test 08 — Adding a small text file produces an upload

**Goal**: end-to-end encrypted write reaches the relay.

**Preconditions**: Test 07 PASS. Headless dev instance running and
unlocked (the watcher lives in the receiver process —
`vault_filesystem_watcher.py`, `vault_runtime_watchers.py`).

**Steps**:
1. Snapshot baseline: `sqlite3 server/data/connector.db
   'SELECT count(*) FROM vault_chunks'`.
2. Drop a small file into the bound folder:
   `printf 'hello vault\n' > /tmp/dc-vault-test-A/hello.txt`.
3. Wait up to 15 s. Poll the chunks count every 2 s; expect it to
   increase.
4. Open `vault-browser` window. Sleep 1.5 s. Screenshot
   `01-browser-with-hello.png`.
5. Dump tree → save as `02-attree.txt`.

**Assertions**:
- `vault_chunks` count increased by ≥ 1.
- `vault_manifests` row count for this vault id is ≥ 1 (or revision
  bumped).
- Browser window lists `hello.txt` with size ~12 bytes.
- Dev desktop log shows a `vault.upload.*` event for the file.
- Dev server log shows the corresponding chunk PUT event.

**Capture**: screenshot + tree dump + DB row counts before/after.

---

## Test 09 — Locking the vault while the browser is open hides plaintext

**Goal**: locked-state UI invariant. No stale plaintext or filenames
should remain visible after a lock event.

**Preconditions**: Test 08 PASS. Browser is open showing
`hello.txt`. Vault is unlocked.

**Steps**:
1. Screenshot `01-browser-pre-lock.png` for baseline.
2. Trigger a lock. Two acceptable triggers (the test uses whichever
   the UI exposes — look it up in the dump tree first):
   a. A "Lock vault" action on `vault-main`.
   b. Killing the headless dev instance
      (`pkill -f "src.main.*desktop-connector-dev"`) — the watcher
      and runtime stop and the browser must reflect that.
3. Wait up to 5 s.
4. Screenshot `02-browser-post-lock.png`.
5. Dump tree → save as `03-attree-post-lock.txt`.

**Assertions**:
- Post-lock dump tree does **not** contain the filename
  `hello.txt`, **OR** the browser visibly transitions to a locked
  state (banner, overlay, or empty list with explanatory copy).
- No tracebacks on browser stderr.
- If trigger (a) was used, dev server log shows no further upload
  events after the lock.

**Capture**: 2 screenshots + post-lock tree dump.

---

## Test 10 — Activity tab populates with producer events

**Goal**: Phase 4 stabilization of `docs/plans/activity-timeline.md`.
Verify the producer side actually lands op-log entries that the
Activity tab consumes — the live counterpart of
`FetchUnifiedManifestIntegrationTests`. Catches a regression that
unit tests against fakes would miss (real AEAD round-trip, real
relay state, real GTK render).

**Preconditions**: Test 08 PASS. Vault is unlocked and contains
`hello.txt` from Test 08. The Test 08 upload itself should have
written one `vault.upload.completed` entry to the shard's
`operation_log_tail` (Phase 2 wiring).

**Steps**:
1. Drive a second small upload so the timeline has > 1 entry:
   - `echo "second line" > <local_bound_folder>/world.txt`
   - Wait up to 5 s for the sync cycle to publish.
2. Open the Vault Settings window:
   - `python3 -m src.windows vault-main --config-dir=~/.config/desktop-connector-dev`
   - (Or activate it via the running tray, depending on whether the
     headless twin's tray is up.)
3. Switch to the **Activity** tab.
4. Screenshot `01-activity-tab.png`. Dump tree → `02-attree.txt`.
5. Press the **Refresh** button (or trigger a re-render).
6. Trigger a delete: in the Vault Browser window, delete
   `hello.txt`. Wait ≤ 5 s, return to Activity tab.
7. Press **Refresh**. Screenshot `03-activity-after-delete.png`.

**Assertions**:
- Activity tab shows at least 3 rows (Test 08 upload + step 1 upload
  + step 6 delete), labelled per `state/activity._EVENT_TYPE_LABELS`:
  - "Uploaded" rows for `hello.txt` and `world.txt`
  - "Deleted" row for `hello.txt`
- Each row carries a timestamp within the test window.
- Each row carries the truncated device-id (no `device_name` yet —
  that's the Phase 3.1 follow-up).
- The status label reads "N event(s)." with N matching the visible
  row count.
- No tracebacks on either dev-desktop stderr or the Activity tab
  worker thread (check `~/.config/desktop-connector-dev/logs/vault.log`).
- The dev server log shows `shard-with-root` publishes corresponding
  to each of the three operations.

**Capture**: 2 screenshots + 1 tree dump + tail of `vault.log`
(`tail -50 ~/.config/desktop-connector-dev/logs/vault.log >
04-vault-log-tail.txt`).

**Why this matters**: Phase 1–3 of `docs/plans/activity-timeline.md`
wired the producer side; this test is the live equivalent of
`tests/protocol/test_desktop_vault_binding_batched_publish.py::FetchUnifiedManifestIntegrationTests`.
A regression that drops shard-tail merge in `assemble_unified_manifest`
or breaks producer wiring at any of the publish sites surfaces as
an empty Activity tab here even though unit tests pass.

---

## End of suite

After test 10 (or whenever the user says "stop"):

1. Run the **Teardown** commands.
2. Write `temp/automation-tests-results/<NNNN>/SUITE.md` summarising
   pass/fail per test plus any cross-test observations.
3. Confirm to the user: "Suite NNNN complete: X/10 pass. Results in
   temp/automation-tests-results/NNNN/."

The dev config dir and dev server DB are **left in place** so the
user can inspect them.
