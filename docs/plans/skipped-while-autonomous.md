# Skipped while running autonomously

Items I touched (or wanted to touch) during an unattended session but couldn't ship without user input. Each entry lists the why-skipped, what design choices are open, and a suggested resolution so a follow-up session can pick up cold.

Last updated: 2026-05-18.

---

## Pre-known deferrals (named in the plan)

### §3.L4 — Permanently-failed ops UI (banner + queue inspector)

**Source:** `docs/plans/unfinished.md` §2 deferred Lows.

**Why skipped:** UX-design-heavy. The reviewer flagged this as "skipped autonomously" precisely because the design choices need user judgement.

**Design questions:**
- Where does the banner sit? Top of Folders tab, or app-wide Vault Settings header, or a global app banner?
- What does the queue inspector look like? Modal dialog, side panel inside Vault Settings, or a new "Queue" tab?
- What per-op actions are exposed? Retry / drop / "report to log" / view full error / restart from baseline?
- Should the inspector show *all* queued ops (including in-flight + waiting) or only the permanently-failed ones?

**Suggested resolution:** half-hour design sync to pick the surface + level of detail. Then 1–2 days to build (banner + queue inspector + ops-list rendering). Code anchors: `vault/binding/sync.py` MAX_OP_ATTEMPTS=10 logic, `vault_folders_tab.py` for banner placement, new `tab_queue.py` inside `windows_vault/`.

---

### §6.L5 — Subprocess crash detection + tray re-open offer

**Source:** `docs/plans/unfinished.md` §2 deferred Lows (§6 verified-clean acknowledgements).

**Why skipped:** Larger than it looks. The tray uses fire-and-forget `subprocess.Popen` at `desktop/src/windows_settings/group_vault.py:96` (and many sibling sites in `tray.py`). Adding crash detection requires:
1. Storing every Popen handle when spawning a window.
2. A background watcher thread that polls `returncode` per handle.
3. A notification surface on non-zero exit.

**Design questions:**
- Notification UX: toast (notify-send), modal dialog, tray-icon badge, or all three?
- "Re-open"? If yes, relaunch with the same args? What about repeat-crash protection (don't loop forever)?
- Scope: only the vault subprocesses (`vault-onboard`, `vault-main`, `vault-browser`, `vault-import`, `vault-export`, `vault-join`, `vault-migration`, `vault-rotate`, `vault-passphrase-generator`), or every GTK4 window the tray spawns?
- Where does the watcher thread live — a singleton in `tray.py` or a small helper in `bootstrap/`?

**Suggested resolution:** 1 day of work once the UX shape is picked. The most defensible scope is "only vault subprocesses, notify via notify-send only, never auto-relaunch". Code anchors: every `subprocess.Popen` call site in `desktop/src/` (grep for `Popen(`), plus `notifications.py` for the toast surface.

---

### B6 — Concurrent edits with binding sync (Tier 2)

**Source:** `docs/plans/live-testing-followup.md` Backlog.

**Why skipped:** Needs multi-device harness scaffolding. Current `docs/testing/vault-tests.md` is single-twin only; lines 129–142 forbid running the headless dev twin in parallel against `php -S` because the long-poll blocks UI traffic. Building the second-twin scaffolding is itself substantive work (separate keyring service, separate config dir, separate pairing, second relay-or-coordinated-pairing).

**Design questions:**
- Two dev twins against one relay vs. one dev twin against each of two relays? The bug is two-paired-devices-on-one-relay, so single relay is correct, but `php -S` starves.
- Switch local relay to `Apache mod_php` or `nginx + php-fpm` for the harness? Or run two `php -S` instances against the same SQLite DB (risky — SQLite has WAL but stress-tests would deadlock)?
- Twin-2 invocation: needs `--config-dir=~/.config/desktop-connector-dev-2` + a *different* keyring service name. `Config` auto-derives this from `config_dir.name`, so it's only a naming-discipline problem.

**Suggested resolution:** 1–2 days to build the harness, then 2–3 hours per Tier 2 multi-device test (B6, and any future B-series item that's inherently multi-device). The first deliverable would be a harness update to `docs/testing/vault-tests.md` setup block + a wrapper script `~/temp-scripts/<NNN>-spin-dev-twin-2.sh` mirroring the existing single-twin setup.

---

### Android radio tail cost — awaiting `_10.txt` dumpsys

**Source:** `docs/plans/android-radio-tail-cost.md`.

**Why skipped:** Needs user-side action — capture `adb shell dumpsys batterystats` from the phone after a ≥6h idle window post-`bf83c67`. Cannot be done autonomously.

**Suggested resolution:** when the user grabs the next dumpsys, paste it (or its path) into a fresh session. The doc's "What `_10.txt` should show" section is the target.

---

### Webcam QR scanning for `windows_vault_join.py`

**Source:** `docs/plans/vault-v1-build-items.md` §5.C2 follow-up; `docs/plans/unfinished.md` cross-reference.

**Why skipped:** v1.x feature, needs design choices that exceed autonomous scope.

**Design questions:**
- Library: `pyzbar` is the obvious choice; needs the `libzbar0` system package as a dep. Adds C-library dep to the AppImage build.
- Wayland portal plumbing: webcam capture on Wayland uses `xdg-desktop-portal-camera`. GTK4 has `GstPlayer` but not a first-class camera widget. Likely path: `Gst.parse_launch("v4l2src ! videoconvert ! ...")` for X11; `pipewiresrc` for Wayland; check if `gstreamer-plugins-good` is on the AppImage already.
- Fallback path: keep paste-URL as primary, scan as opt-in. Don't auto-open camera on dialog open (privacy).

**Suggested resolution:** 2–3 days when v1 ships and we move to v1.x. Bundle with any other camera-touching feature to amortize the dep cost.

---

## Newly-discovered items (added during autonomous run)

*(none yet — the §3.5 sweep itself was clean; the legacy helpers were dropped without surprises.)*

---

## Partial work / time-budget overruns

### Live tests B3, B2, B5, B4, B1 — deferred from the plan's Step 2–6

**Why skipped:** Step 1 (the §3.5 test-fixture sweep across 22 files + final helper drop = 23 commits) consumed the bulk of the session budget. Doing five live tests across the remainder reliably enough to write up trustworthy findings was a higher risk than skipping them cleanly with concrete pickup recipes.

Each live test below has a self-contained run recipe so a follow-up 1–2-hour session can pick it up without re-deriving context. Order is the original plan order (B3 → B2 → B5 → B4 → B1).

#### B3 — Migration switch-back live test

**Status:** not run. The §5.C1 migration wizard (commit `f6b04feXX` series, 2026-05-18) is pinned by source tests (`tests/protocol/test_desktop_vault_migration_wizard_source.py`) and engine tests (`tests/protocol/test_desktop_vault_migration_runner.py`); the live drive against two real PHP relays remains the only thing not exercised.

**Setup recipe:**
```bash
cd /home/mhavranek/git/desktop-connector

# Wipe + start relay A on 4441 (canonical dev relay).
rm -f server/data/connector.db
rm -rf server/storage/* server/data/logs
mkdir -p server/data/logs server/storage
php -S 127.0.0.1:4441 -t server/public/ > server/data/logs/relay-a.log 2>&1 &
echo $! > /tmp/relay-a.pid

# Spin a second relay on 4442 with an isolated storage tree.
mkdir -p /tmp/dc-relay-b/data/logs /tmp/dc-relay-b/storage
cp -r server/public /tmp/dc-relay-b/
# Reuse the same schema; relay B starts empty.
php -S 127.0.0.1:4442 -t /tmp/dc-relay-b/public/ > /tmp/dc-relay-b/data/logs/relay-b.log 2>&1 &
echo $! > /tmp/relay-b.pid
```

**Test flow:**
1. Spin dev twin against relay A: `cd desktop && DC_ALLOW_MULTI_INSTANCE=1 python3 -m src.main --config-dir=~/.config/desktop-connector-dev --server-url=http://127.0.0.1:4441`.
2. Onboard a fresh vault. Add the `~/Documents/dc-dev-test-folder/` binding with 3 test files. Wait for upload to land (Activity tab shows green).
3. Open Vault Settings → Migration → "Migrate to another relay…". Enter `http://127.0.0.1:4442`. Drive setup → confirm → progress → done.
4. Verify all 3 files landed on relay B: `sqlite3 /tmp/dc-relay-b/data/connector.db "SELECT vault_id, root_revision FROM vault_roots"`. Should show the same root_revision as relay A had at the end of step 2.
5. Verify the Migration tab in Vault Settings shows the "switch back to relay A" affordance (visible during the post-commit grace window).
6. Click "Switch back". Drive the wizard back. Verify A's root_revision is one higher than B's (the switch-back republishes).
7. To verify the §5.M6 grace-window cleanup: edit `~/.config/desktop-connector-dev/config.json`, set `vault_previous_relay_expires_at` to a past RFC3339 timestamp, reopen the Migration tab. The switch-back affordance should be gone.

**Write up findings as §14 in `docs/plans/live-testing-followup.md`** using the Symptom / Cause / Fix shape / Acceptance / Status template (matches §1–§13).

#### B2 — Debug bundle leak scan on a real install

**Status:** not run. Existing unit coverage in `tests/protocol/test_desktop_vault_debug_bundle.py` is broad (25 tests across redaction, scan, schema dump, build pipeline). The live test would verify the end-to-end bundle from a realistic dev-twin state including real auth_token + vault grant + history.

**Setup recipe:**
```bash
# Dev twin should already have an onboarded vault + a paired device.
# If not, run the standard onboarding (vault-onboard) + pairing first.

cd /home/mhavranek/git/desktop-connector/desktop
DC_ALLOW_MULTI_INSTANCE=1 python3 -m src.main --config-dir=~/.config/desktop-connector-dev --server-url=http://127.0.0.1:4441
# In tray: Vault Settings → Maintenance → "Download debug bundle".
# It lands at ~/Downloads/desktop-connector-debug-<timestamp>.zip.
```

**Test flow:**
1. Capture the dev twin's secrets BEFORE generating the bundle: `cat ~/.config/desktop-connector-dev/config.json | jq '.auth_token, .vault_access_secret'`; `secret-tool lookup service desktop-connector-dev`.
2. Generate the bundle via the UI.
3. Decompress: `mkdir /tmp/bundle-inspect && unzip ~/Downloads/desktop-connector-debug-*.zip -d /tmp/bundle-inspect/`.
4. Leak scan:
   - `grep -r "<auth_token from step 1>" /tmp/bundle-inspect/` — expect 0 matches.
   - `grep -r "<vault_access_secret>" /tmp/bundle-inspect/` — expect 0 matches.
   - `grep -rE "Bearer [A-Za-z0-9._/-]{16,}" /tmp/bundle-inspect/` — expect 0 matches (only `<redacted>` placeholders).
   - `grep -rE "[A-Za-z0-9+/]{43,44}={0,2}" /tmp/bundle-inspect/` — base64-shaped 32-byte runs; manually verify any hit isn't a real secret.
5. Verify ZIP contents: should include `config.redacted.json`, `index_schema.txt`, `binding_states.json`, `activity_tail.txt`, `manifest_summary.json`. No `keys/`, `history.json`, `recovery_kit.bin`, or raw `config.json`.

**Write up as §15 in `live-testing-followup.md`.** Status field: `done` if zero real-secret leaks, `partial — leaks at <file>` otherwise (then file follow-up entries here).

#### B5 — Eviction under quota pressure live test

**Status:** not run. The §3.C1 eviction implementation landed 2026-05-18 (`vault/ops/eviction.py`); unit coverage is in `tests/protocol/test_desktop_vault_eviction.py`. Live test stresses the full upload+alarm+purge cycle against a real low-quota relay.

**Setup recipe:**
```bash
# Edit relay A's config to set a tiny quota.
cat > server/data/config.json <<EOF
{ "vaultQuotaBytes": 4194304 }
EOF
# Restart relay A so the new config takes effect.
kill $(cat /tmp/relay-a.pid) 2>/dev/null
php -S 127.0.0.1:4441 -t server/public/ > server/data/logs/relay-a.log 2>&1 &
echo $! > /tmp/relay-a.pid
```

**Test flow:**
1. On the dev twin, bind a folder with 5 files of 1 MiB each. Confirm all 5 land (relay quota now nearly full).
2. Add a 6th 1 MiB file to the folder. Watch for the eviction alarm dialog (passphrase prompt).
3. Enter the dev twin passphrase. Eviction should purge the oldest file's chunks (look for `vault.eviction.alarm_purged_oldest` in `vault.log`).
4. Verify the Activity tab shows the eviction as a destructive event.
5. Use Vault Browser → "Show deleted" toggle to verify the evicted file surfaces as a tombstone.
6. Confirm restore works: pick a tombstone → Restore → file content reappears (chunk dedup means no re-upload).

**Write up as §16 in `live-testing-followup.md`.**

#### B4 — Ransomware detector trip live test

**Status:** not run. The detector lives in `vault/binding/ransomware_detector.py`; default trigger is "200 changes / 5 min OR ≥50 % rename ratio." Coverage exists in unit tests; the live test exercises the full sync-pause + UI-warning path.

**Setup recipe:**
```bash
# Create a folder with 100 files for the dev twin.
mkdir -p ~/dc-dev-bound-folder
for i in $(seq 1 100); do
  echo "file $i content" > ~/dc-dev-bound-folder/file-$i.txt
done

# In the dev twin: bind this folder. Wait for initial upload to settle.
```

**Test flow:**
1. With sync running steady-state, simulate ransomware: rename every file in one burst:
   ```bash
   cd ~/dc-dev-bound-folder
   for f in *.txt; do mv "$f" "${f%.txt}.enc"; done
   ```
2. The detector should fire within ~5 sec (binding's watcher sees 100 renames; ratio = 100 %).
3. Expected log line: `vault.sync.ransomware_pause_triggered binding=... title='Suspicious mass change detected' body='Vault sync has been paused for this folder...'`.
4. Verify the UI surfaces the warning (likely a banner in Vault Settings → Folders tab on the affected binding's row).
5. Click "Review changes" / "Resume" and verify the unwind flow handles the 100 renames sensibly (either uploads them as new files, or surfaces a per-file conflict UI).

**Write up as §17 in `live-testing-followup.md`.**

#### B1 — Schedule purge live test

**Status:** not run. The scheduled-purge auto-executor decision (§6.H1 ADR 2026-05-18) is "fire-on-attended", so this test must verify that an attended autosync tick fires a due purge correctly. Requires clock-mocking or `faketime`.

**Setup recipe:**
1. Check whether `faketime` is installed (`which faketime`). If not: `~/temp-scripts/<NNN>-install-faketime.sh` to apt-install `faketime`.
2. Schedule a purge from the dev twin: Vault Settings → Danger → "Schedule full purge" → pick a date 7 days in the future.
3. Stop the dev twin.
4. Restart with faketime advancing system time past the purge date: `faketime '8 days' python3 -m src.main --config-dir=...`.
5. Open Vault Settings to trigger an attended autosync tick.
6. Verify the purge fires and audits correctly (look for `vault.eviction.scheduled_purge_executed` in vault.log).

**Skip cleanly to `skipped-while-autonomous.md`** if:
- `faketime` isn't installed (needs sudo to install — user-driven).
- The dev twin doesn't load purges from disk on startup (then the test requires staying within one process).

**Write up as §18 in `live-testing-followup.md`.**

### Migration wizard UI dogtail drive — needs human supervision

**Why skipped:** The B3 test as scripted above drives the GTK wizard end-to-end. While dogtail can in principle drive `Adw.MessageDialog` pages, multi-page wizards with worker-thread state transitions (the migration wizard runs Argon2id + 50× chunk fetches on a background thread) are difficult to drive reliably from a script without race conditions. Memory pin: `feedback_no_fake_tests.md` says verify buttons must do real round-trip work. A flaky dogtail run that "passes" with empty assertions is worse than no test.

**Suggested resolution:** a 30-minute session where the user drives the wizard manually while the autonomous session captures screenshots + log tail in parallel. Then writeup is straightforward (UX observations + log evidence + Acceptance).

