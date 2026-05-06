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
5. `mcp__gtk-a11y__screenshot` of the window —
   `screenshots/01-vault-main-empty.png`.
6. Close the window: `pkill -f "src.windows vault-main.*desktop-connector-dev"`.

**Assertions**:
- A window with title containing "Vault" is in the AT-SPI app list.
- The dump tree contains the string "(no vault opened)" or "(no
  vault)" — i.e. the empty-state copy from `windows_vault.py:93,1164`.
- No Python tracebacks on stderr.

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
  `success` (per state machine in `windows_vault.py:1577,1697,2105`).
- `sqlite3 server/data/connector.db 'SELECT count(*) FROM vaults'` = 1.
- `~/.config/desktop-connector-dev/config.json` has a vault id field
  populated (search for keys mentioning `vault`).
- Dev server log contains a `vault.create.*` success event.

**Capture**: 3 screenshots, server-log excerpt with `vault.*` events
filtered.

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
- The Recovery / Folders / Devices / Activity / Maintenance / Security
  tabs (from `windows_vault.py:59`) are present.

**Capture**: screenshot + AT-SPI tree.

---

## Test 06 — Lock + relock cycle through the headless runtime

**Goal**: the AEAD header round-trips. Locking forces re-derivation
on next unlock.

**Preconditions**: Test 05 PASS.

**Steps**:
1. Start the headless dev instance (from test 02). Sleep 2 s.
2. Watch `~/.config/desktop-connector-dev/logs/desktop-connector.log`.
3. Issue a "lock" by stopping the dev instance (`pkill -f
   "src.main.*desktop-connector-dev"`). Sleep 1 s.
4. Restart the dev instance with the same args. The vault should
   start in the locked state (no plaintext key material in memory
   at startup).
5. Verify the log shows the vault as **locked** at boot.
6. Drive an unlock through the GTK window: launch `vault-main`,
   click an "Unlock" button (look it up in dump tree), enter the
   same fixed passphrase, click confirm.
7. Wait up to 5 s for an "unlocked" indicator.

**Assertions**:
- Boot log shows a `vault.lock.*` or `vault.locked.*` event before
  any unlock event.
- Post-unlock log shows a `vault.unlock.succeeded` (or equivalent)
  event with the same vault id from test 04.
- The unlock used Argon2id (look for `vault.kdf.argon2id` events).

**Capture**: pre/post screenshots + log excerpt.

---

## Test 07 — Wrong passphrase is rejected, vault stays locked

**Goal**: negative path. A wrong passphrase must produce a visible
error and **must not** quietly succeed nor crash.

**Preconditions**: Test 06 PASS. Vault is unlocked. Lock it again
first (kill + restart dev instance per test 06 step 3–4).

**Steps**:
1. Open `vault-main`. Find the unlock entry.
2. Type a deliberately wrong passphrase
   (`zzzz wrong passphrase abc def ghi`).
3. Click confirm.
4. Sleep 2 s (Argon2id is slow — leave headroom).
5. Screenshot `01-wrong-passphrase-error.png`.
6. Dump tree → save as `02-attree.txt`.
7. Close window.

**Assertions**:
- An error label/banner is visible (search dump tree for "Wrong",
  "incorrect", "failed", or `EA7601` in CSS classes).
- The vault is **still locked**: subsequent dev-instance log shows no
  `vault.unlock.succeeded`.
- No Python tracebacks on stderr.
- The dev server log shows **no** new `vault.*` records associated
  with the wrong attempt that would indicate accidental decrypt.

**Capture**: screenshot + tree dump + log excerpt covering the
attempt window.

---

## Test 08 — Bind a folder to the vault

**Goal**: first state-creating action against an unlocked vault.

**Preconditions**: Test 07 PASS. Unlock the vault again
(driver: same flow as test 06 step 6).

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

## Test 09 — Adding a small text file produces an upload

**Goal**: end-to-end encrypted write reaches the relay.

**Preconditions**: Test 08 PASS. Headless dev instance running and
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

## Test 10 — Locking the vault while the browser is open hides plaintext

**Goal**: locked-state UI invariant. No stale plaintext or filenames
should remain visible after a lock event.

**Preconditions**: Test 09 PASS. Browser is open showing
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

## End of suite

After test 10 (or whenever the user says "stop"):

1. Run the **Teardown** commands.
2. Write `temp/automation-tests-results/<NNNN>/SUITE.md` summarising
   pass/fail per test plus any cross-test observations.
3. Confirm to the user: "Suite NNNN complete: X/10 pass. Results in
   temp/automation-tests-results/NNNN/."

The dev config dir and dev server DB are **left in place** so the
user can inspect them.
