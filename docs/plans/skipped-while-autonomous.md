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

*(none yet — will be filled in as the session progresses)*

---

## Partial work / time-budget overruns

*(none yet)*
