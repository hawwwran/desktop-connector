# Desktop multi-device support

Status: M.0 – M.11 complete. M.0–M.10 landed 2026-04-29 / 2026-04-30;
M.11 (desktop-to-desktop pairing bootstrap) landed 2026-04-30 on the
same branch. Manual GTK smoke + lock-screen matrix remain as
documented follow-ups under M.9 / M.10.

Branch: `desktop-multi-device-support`

Execution model: this is a live implementation ledger. Work one `M.X` chunk at a
time, update the chunk status and verification notes before moving to the next
chunk.

Scope: desktop app support for multiple paired connected devices. Android
already has multi-pair support and is the reference for selection, per-peer
history, targeted send, and find-device command handling. The relay already
supports multiple pairings per device.

## Goals

1. Replace user-facing "phone" wording with "device" or "connected device"
   where the target may be a phone, tablet, desktop, or laptop. Keep existing
   wire names where changing them would break compatibility.
2. Support more than one paired device on desktop without funneling send,
   history, status, or find actions through the first paired row.
3. Define one persistent "active device": the last paired device that sent this
   desktop an item, or the last paired device this desktop sent an item to.
4. Add device pickers to:
   - History window.
   - `Send files to` window.
   - `Find my device` window.
5. Default every picker to the active device. If there is no active device,
   default to the newest paired device, then the first legacy pair.
6. Add a naming step after pairing verification. The default value is
   `Device X`, where `X = number of currently paired devices + 1`. Device
   names must be unique.
7. Install one file-manager send target per paired device, named from the
   paired device name, for Nautilus/Nemo and Dolphin where supported.
8. Remove generated file-manager send targets for a device when that device is
   unpaired.
9. Rename "Find my phone" to "Find my device", keep the target selector, and
   add desktop-side support for being found.

## Non-goals

1. Do not change the server pairing cardinality model.
2. Do not break existing Android clients or old desktop clients that still use
   `fn=find-phone` fasttrack payloads.
3. Do not require lock-screen UI support in the main implementation. Lock-screen
   behavior is a later hardening concern because it may need compositor,
   session, or systemd-user work beyond normal app windows.

## Design Decisions

### D1. Central connected-device registry

Desktop already persists `paired_devices` as a dictionary in `Config`, but many
call sites still call `get_first_paired_device()`. Add a small desktop-side
registry module, likely `desktop/src/devices.py`, that wraps `Config` and owns:

- `list_devices()` sorted by `paired_at` descending.
- `get(device_id)`.
- `active_device_id` persisted in config as `active_device_id`.
- `get_active_device()` with fallback to newest paired, then first legacy pair.
- `mark_active(device_id, reason)`.
- `next_default_name()` starting at current paired-device count plus one and
  incrementing only if that `Device X` is already taken.
- `rename(device_id, name)` with unique-name validation.
- `unpair(device_id)` forwarding through `Config.remove_paired_device()`.

Keep secret handling in `Config`; the registry must not copy symmetric keys
back into JSON.

### D2. Active device semantics

The active device changes only when actual device activity happens:

- Incoming valid transfer or command from paired device `X`: mark `X` active.
- Outgoing file or clipboard send to paired device `X`: mark `X` active once
  the send is accepted into the send flow.
- Find-device command to `X`: mark `X` active only after the command is
  successfully queued, because it is a directed device action.

Opening a picker and changing the visible filter does not by itself change the
active device. `Find my device` is the exception in user workflow, not storage:
the user must choose the target before starting, the selected target is used for
the locate session, and the selector is disabled while locating is in progress.
The target becomes active after the start command is successfully queued.

### D3. History attribution

Add `peer_device_id` to every new history item. It means "the other device":

- Sent rows: target device id.
- Received rows: sender device id.

Keep `sender_id` on received rows for compatibility with existing code. Legacy
history rows without `peer_device_id` remain readable. For display filtering,
best-effort legacy attribution can fall back to `sender_id`, then active
device, then first paired device, but new rows must always write
`peer_device_id`.

### D4. File-manager send targets

Nautilus and Nemo scripts are flat executable files, so they need one generated
script per paired device, for example `Send to Device 1`. Dolphin can use a
single service-menu file with one action per device.

Scripts should pass an opaque device id to the app, not a display name:

```text
desktop-connector --headless --send=/path/file --target-device-id=<peer-id>
```

Generated files need a clear managed marker containing the paired device id.
The sync code removes stale managed files for unpaired devices and does not
delete user-created files without the marker. Since device names are unique,
generated labels normally map directly to names. If duplicated names are found
because of legacy state, manual config edits, or a race, normalize them first by
appending a short device id to the later duplicate and saving that corrected
name before generating file-manager entries.

### D5. Find-device wire compatibility

Keep accepting and sending the existing fasttrack payload shape:

```json
{"fn":"find-phone","action":"start"}
{"fn":"find-phone","action":"stop"}
{"fn":"find-phone","state":"ringing"}
{"fn":"find-phone","state":"stopped"}
```

User-facing UI and diagnostics can say "find device". The desktop receiver
should accept both `find-phone` and `find-device` if a new alias is added, but
the default sender should stay compatible with Android until both platforms are
explicitly migrated.

### D6. Desktop being found

Desktop needs a background fasttrack consumer in the normal poller process.
When a paired sender asks to locate this desktop:

- If non-silent: show an always-on-top best-effort modal saying this device is
  being located, and play a repeating alert sound until stopped or timed out.
- If silent: do not show the modal and do not play sound.
- If a position can be determined, send `lat`, `lng`, and `accuracy` updates
  back through encrypted fasttrack messages. Never log coordinates.
- If no position can be determined, still send a heartbeat state so the sender
  can show that the device received the command.
- If a second different sender starts locating while one is active, use the
  Android rule: first active request wins, later starts are dropped and logged
  without location data.

### D7. Unique device names

Do not allow the user to save a duplicate connected-device name. Pairing and
Settings rename flows both validate against every other paired device name,
case-insensitively after trimming whitespace.

If duplicate names already exist when the app starts or before file-manager
sync runs, enforce the policy automatically by renaming the later duplicate to
`<name> <short-id>` and persisting it. This keeps Settings, pickers, and
file-manager send targets aligned around the same unique names.

### D8. Desktop-to-desktop pairing — relay-server constraint (M.11)

Two desktops being introduced through the M.11 paste flow MUST already be
configured for the same relay server. The pasted bootstrap blob carries the
inviter's `server` URL; the joiner refuses on mismatch with a clear error
rather than silently switching the user's relay. The paired-different-relay
model isn't supported anywhere else in the system, and a silent relay
switch on the joiner would surprise the user.

Comparison is normalized: trailing slash trimmed, scheme + host
case-insensitive. Path is compared verbatim (relays in subdirectories like
`/SERVICES/desktop-connector` need to match exactly).

### D9. Pairing-key format (M.11)

Single canonical content, shipped through two interchangeable channels —
**a text string** (for copy-paste through chat / email / password manager)
and **a file** (for sneaker-net via USB stick, syncthing folder, or any
sync app). The user picks whichever channel they have available; the
parser handles both.

**Content** is the existing QR JSON shape (`{server, device_id, pubkey,
name}`) base64 (URL-safe, no padding) and prefixed with the magic
sentinel `dc-pair:` so it's recognisable when pasted into a chat
window or grep'd in a backup directory:

```
dc-pair:eyJzZXJ2ZXIiOiJodHRwczovLy4uLiIsImRldmljZV9pZCI6Ii4uLiIsInB1YmtleSI6Ii4uLiIsIm5hbWUiOiIuLi4ifQ
```

**Text channel** ("Show pairing key" / "Enter pairing key"): the literal
single-line `dc-pair:<base64>` string. Always one line, no whitespace —
users can paste through line-wrapping channels (chat, email body) and
the parser strips trailing whitespace + newlines defensively.

**File channel** ("Export pairing key" / "Import pairing key"): a
text file with extension `.dcpair`, MIME `application/x-desktop-connector-pairing`.
File contents are exactly the `dc-pair:<base64>` string — same parser,
same validation. Default filename when exporting:
`<my-device-name>.dcpair` (sanitised) so the joiner sees who it came
from before opening it.

**Parser** is single-pass for both channels:

1. Trim whitespace + newlines.
2. Strip leading `dc-pair:` if present (file path callers should always
   have the prefix; defensive parsing also accepts bare base64 for
   forgiving paste UX).
3. URL-safe base64 decode.
4. JSON parse.
5. Required keys present + correctly typed (`server`: str URL,
   `device_id`: str, `pubkey`: str b64, `name`: str non-empty).
6. Apply D8 + D10 validation rules.

**Threat model**: the pairing key is paste-secret material in the same
bucket as the QR image. Anyone who holds the key can pretend to be the
requester to that specific inviter for a short window (until the
inviter closes the pairing window or confirms a different request).
The verification-code step on both sides catches MITM. The key
carries no symmetric key — the symkey is derived per-pair from ECDH
on each side from its own private key plus the other side's pubkey.

The export/show dialogs surface this in copy: "This key contains your
relay address and your desktop's public identifier. Anyone with the
key can request to pair with this desktop while the pairing window is
open. Verify the code on both screens before confirming."

### D10. Desktop-to-desktop pairing — request endpoint (M.11)

Reuses the existing `POST /api/pairing/request` and `GET /api/pairing/poll`
+ `POST /api/pairing/confirm` endpoints unchanged. The wire field name
`phone_pubkey` is functionally "requester pubkey" regardless of caller
role. M.11 documents that retention in
`docs/protocol.compatibility.md` and `docs/protocol/protocol.md` rather
than renaming the wire field. No server changes needed.

The joiner side derives the shared key locally as soon as the blob
parses; the verification code displays in parallel with the inviter
seeing the same code from its poll loop. Either side clicking Confirm
saves its own local pairing independently. If the inviter never
confirms, the server-side `pairings` row never lands and transfers fail
with the existing 401/403 auth-failure recovery banner.

## Chunks

### M.0 - Connected-device state model

Status: completed 2026-04-30

Goal: add the desktop abstraction that removes "first paired device" from new
work.

Work:

- Add `desktop/src/devices.py` with a connected-device dataclass and registry.
- Add `Config.active_device_id` getter/setter or equivalent storage helpers.
- Add `Config.rename_paired_device(device_id, name)` for Settings rename and
  pairing-name save.
- Add a default-name helper that starts at current paired-device count plus one
  and increments only if that `Device X` is already taken.
- Add duplicate-name normalization that appends a short device id and persists
  the corrected name if legacy/corrupt state already contains duplicates.
- Add unit tests for sorting, active fallback, stale active id cleanup, default
  naming, duplicate-name rejection/normalization, and no plaintext secret
  regression.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_multi_device_config`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_secrets` passed
  2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_receive_actions_config`
  passed 2026-04-30.
- `git diff --check` passed 2026-04-30.

### M.1 - History peer attribution and active tracking

Status: completed 2026-04-30

Goal: make history rows targetable by connected device and update active device
from real transfer activity.

Work:

- Extend `TransferHistory.add(...)` with `peer_device_id`.
- Write `peer_device_id` in desktop receive paths for file, streaming file, and
  `.fn.*` command transfers.
- Write `peer_device_id` in desktop send paths: GTK send window, tray clipboard,
  and `--send`.
- Mark active on valid incoming sender and outgoing target.
- Add read-side legacy fallback so older rows without `peer_device_id` still
  render.
- Ensure delivery tracker and delete/cancel logic still find rows by
  `transfer_id`.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_history_multi_device`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_history_streaming` passed
  2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_send_runner_streaming`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_streaming_recipient`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_receive_actions_poller`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_multi_device_config`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_config_permissions` passed
  2026-04-30.
- `git diff --check` passed 2026-04-30.

### M.2 - Target resolution and CLI plumbing

Status: completed 2026-04-30

Goal: let non-GTK entry points send to a specific connected device.

Work:

- Add `--target-device-id` to desktop startup args.
- Teach `run_send_file(...)` to resolve target by explicit id, then active
  device fallback.
- Return a clear error when the explicit target is not paired.
- Update tray clipboard send to use the active device instead of first pair.
- Update tray remote-status ping to probe the active device.
- Keep `get_first_paired_device()` for legacy callers until all migration work
  is complete, but stop using it in new multi-device paths.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_target_resolution` passed
  2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_send_runner_streaming`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_send_folder_rejection`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_history_multi_device`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_multi_device_config`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_config_permissions` passed
  2026-04-30.
- `git diff --check` passed 2026-04-30.

### M.3 - Send files window

Status: completed 2026-04-30

Goal: rename the GTK send modal and add a target picker.

Work:

- Rename window and labels from `Send to Phone` to `Send files to`.
- Add a shared GTK device picker row near the top of the window.
- Preselect the active device on open.
- Disable send when there are no paired devices.
- Use the selected device id and symmetric key for every file in that send
  batch.
- Mark the selected device active when the batch enters the send flow.
- Keep folder rejection and progress behavior unchanged.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_send_files_multi_device_source`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_send_folder_rejection`
  passed 2026-04-30.
- `python3 -m py_compile desktop/src/windows.py` passed 2026-04-30.
- `git diff --check` passed 2026-04-30.
- Manual GTK smoke not run in this sandbox; prior GTK smoke checks in this
  repo have been environment-gated/unreliable here.

### M.4 - History window device picker and filtering

Status: completed 2026-04-30

Goal: make transfer history device-scoped by default.

Work:

- Add a device picker to the History window header or first row.
- Preselect active device on open.
- Filter rows to items whose `peer_device_id` matches the selected device.
- Keep progress refresh/diffing local to the filtered list.
- Show an empty state for "No transfers with <device name>".
- Clear history removes only the visible history for the selected device, with
  dialog copy naming that device.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_history_multi_device`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_history_multi_device_source`
  passed 2026-04-30.
- `python3 -m py_compile desktop/src/windows.py desktop/src/history.py` passed
  2026-04-30.
- `git diff --check` passed 2026-04-30.
- Manual GTK smoke not run in this sandbox; prior GTK smoke checks in this
  repo have been environment-gated/unreliable here.

### M.5 - Pairing naming and settings pair list

Status: completed 2026-04-30

Goal: make pairing and unpairing multi-device aware.

Work:

- Rename pairing UI copy from "phone" to "device".
- After verification code confirmation, show a naming input before saving the
  local pair and confirming with the server.
- Prefill the input with `Device X`, where `X` uses the current paired-device
  count plus one.
- Reject empty or duplicate names before saving. Duplicate comparison is
  case-insensitive after trimming whitespace.
- Store the chosen unique name in `paired_devices[device_id].name`.
- Mark the newly paired device active.
- Replace Settings' single "Paired Device" section with a list of all connected
  devices.
- Add per-device rename in Settings, matching Android behavior and using the
  same unique-name validation as pairing.
- Add per-device unpair. It should send `.fn.unpair` to that specific device,
  remove only that pairing locally, update active fallback, and trigger
  file-manager script cleanup.
- Sync generated file-manager labels after rename. (Sync helper itself ships
  in M.6; M.5 leaves the rename/unpair state in the registry so M.6's helper
  can read it.)

Verification:

- `python3 -m unittest tests.protocol.test_desktop_pairing_naming` passed
  2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_pairing_window_source`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_settings_multi_device_source`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_multi_device_config`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_history_multi_device`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_send_files_multi_device_source`
  passed 2026-04-30.
- `python3 -m py_compile desktop/src/windows.py desktop/src/pairing.py`
  passed 2026-04-30.
- `git diff --check` passed 2026-04-30.
- Manual GTK smoke not run in this sandbox; prior GTK smoke checks in this
  repo have been environment-gated/unreliable here.

### M.6 - File-manager integration per connected device

Status: completed 2026-04-30

Goal: generate and clean file-manager send targets for every paired device.

Work:

- Add a desktop-side integration sync helper, likely
  `desktop/src/file_manager_integration.py`.
- Generate Nautilus/Nemo scripts named `Send to <device name>` with a managed
  marker and fixed `--target-device-id`.
- Generate Dolphin service-menu actions per device.
- Normalize any pre-existing duplicate names before generating scripts so the
  generated filenames/action labels are unique and match Settings.
- Remove stale managed entries on unpair and on startup when the paired-device
  set changed while the app was not running.
- Stop creating new generic `Send to Phone` scripts in AppImage/source install
  hooks. Existing generic scripts from old installs should be removed or
  rewritten by the first sync when they are recognized as ours.
- Trigger sync at startup, after pairing save, after unpair, and after rename.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_file_manager_integration`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_appimage_install_hook`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_send_folder_rejection`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_pairing_naming` passed
  2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_settings_multi_device_source`
  passed 2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_multi_device_config`
  passed 2026-04-30.
- `python3 -m py_compile desktop/src/file_manager_integration.py
  desktop/src/main.py desktop/src/windows.py desktop/src/pairing.py
  desktop/src/bootstrap/appimage_install_hook.py` passed 2026-04-30.
- `bash -n desktop/install-from-source.sh` passed 2026-04-30.
- `git diff --check` passed 2026-04-30.

### M.7 - Find my device sender UI

Status: completed 2026-04-30

Goal: update the existing desktop find window to target any connected device.

Work:

- Rename tray/menu/window labels from `Find my Phone` to `Find my Device`.
- Add the shared device picker.
- Preselect active device on open.
- Require the user to choose the target device before starting the locate
  session.
- Disable the selector while locating is in progress; the target cannot be
  changed until the session stops.
- Use selected target id/key for start, stop, stale fasttrack flush, and
  response decryption.
- Keep current silent search, volume, timeout, map, and lost-communication
  behavior.
- Mark the selected target active after the start command is successfully
  queued.
- Wire payload `fn=find-phone` is unchanged (D5 — Android compat); only
  user-facing copy switches to "device".

Verification:

- `python3 -m unittest tests.protocol.test_desktop_find_device_source` passed
  2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_message_contract` passed
  2026-04-30 — fasttrack contract preserved.
- `python3 -m unittest tests.protocol.test_desktop_history_multi_device_source
  tests.protocol.test_desktop_pairing_window_source
  tests.protocol.test_desktop_settings_multi_device_source
  tests.protocol.test_desktop_send_files_multi_device_source` passed
  2026-04-30 — section-marker rename in `windows.py` did not break sibling
  source-locator tests.
- `python3 -m py_compile desktop/src/windows.py desktop/src/tray.py` passed
  2026-04-30.
- `git diff --check` passed 2026-04-30.
- Manual send-to-Android find-device smoke: not run in this sandbox; the
  fasttrack wire is byte-equivalent to pre-M.7, so existing message-contract
  test coverage exercises it.

### M.8 - Desktop find-device receiver

Status: completed 2026-04-30

Goal: allow this desktop app to be located by another connected device.

Work:

- Add background fasttrack polling to `Poller` without blocking file transfer
  polling. (`Poller._fasttrack_consumer_loop`, separate daemon thread, 8 s
  cadence so the GTK find-device sender window's 3 s loop wins races.)
- Decrypt each fasttrack message with the sender's paired symmetric key.
  Unknown sender / decrypt failure / non-dict payload all ACK + drop.
- Route payloads through `FasttrackAdapter` and `MessageDispatcher`. Adapter
  now accepts both `fn=find-phone` (legacy) and `fn=find-device` (new alias)
  per D5; senders stay on `find-phone` until both platforms migrate.
- Add a `FindDeviceResponder` service for `start`, `stop`, timeout, state
  updates, and concurrency rules. FCFS via `findphone.start.dropped_concurrent`
  matches Android. Same-sender re-start refreshes the session. 5 min hard
  timeout calls `stop()` + sends a final `state="stopped"`. Inbound `stop`
  from non-active sender is silently ignored.
- For non-silent locate requests, play an alert sound and show an always-on-top
  best-effort GTK modal with stop action and sender/device information.
  (`GtkSubprocessAlert` spawns a `locate-alert` GTK4 subprocess + a sound loop
  via paplay/aplay/play/mpv; user clicking Stop exits the subprocess and a
  watcher thread invokes `responder.stop()`.)
- For silent locate requests, skip modal and sound (responder still sends
  state heartbeats so the requester sees we received the command).
- Send encrypted status/location updates back to the requesting device and ACK
  processed fasttrack messages. Coordinates are not yet supplied — M.9 wires
  the location provider; M.8 sends state-only heartbeats so the wire stays
  byte-stable.
- Headless runs (`--headless` receiver) skip the modal entirely; the
  responder still fires heartbeats so a requester sees we're alive.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_find_device_responder`
  passed 2026-04-30 (19 tests: adapter alias dispatch, FCFS drop, same-sender
  refresh, stop-from-active, stop-from-other-ignored, timeout, encrypted
  payload shape, silent-no-alert, dispatcher round-trip).
- `python3 -m unittest tests.protocol.test_desktop_fasttrack_consumer` passed
  2026-04-30 (7 tests: dispatch + ACK, unknown sender drop+ACK, decrypt
  failure drop+ACK, encrypted send shape, coordinate serialization, unpaired
  recipient send rejection, active-device marking on inbound start).
- `python3 -m unittest tests.protocol.test_desktop_message_contract` passed
  2026-04-30 (fasttrack wire shape unchanged for Android compat).
- `python3 -m unittest discover -s tests/protocol -p 'test_desktop_*.py'`
  passed 2026-04-30 (434 / 438 — 4 pre-existing PHP integration test errors
  unrelated to this branch).
- `python3 -m py_compile desktop/src/find_device_responder.py
  desktop/src/find_device_alert.py desktop/src/poller.py
  desktop/src/runners/receiver_runner.py desktop/src/windows.py
  desktop/src/messaging/fasttrack_adapter.py` passed 2026-04-30.
- `git diff --check` passed 2026-04-30.
- Manual desktop-to-desktop smoke: not run in this sandbox (single desktop
  available); covered by the unit-level dispatcher round-trip test plus the
  consumer's decrypt-and-route test on real ciphertext.

### M.9 - Desktop location provider and lock-screen hardening

Status: completed 2026-04-30

Goal: provide best-effort desktop location now and document lock-screen support
as a later hardening concern.

Work:

- Add a location provider abstraction under `desktop/src/interfaces`. New
  `LocationProvider` Protocol + `LocationFix` dataclass + `NullLocationProvider`
  default. Threaded into `DesktopPlatform` (frozen dataclass field with
  default factory) so existing constructions stay source-compatible.
- Linux first pass: `desktop/src/backends/linux/location_backend.py`'s
  `GeoClueLocationProvider` connects lazily on first call, sets up a
  `Gio.DBusProxy` against `org.freedesktop.GeoClue2.Manager`, registers
  `desktop-connector` as the desktop id, requests `RequestedAccuracyLevel = 8`
  (Exact), starts the client, listens for `LocationUpdated` signals on a
  private `GLib.MainLoop` thread, and caches the most recent fix in memory
  under a lock.
- Fallback: every failure path (gi import error, system bus unreachable,
  GeoClue not running, GetClient denied, Start refused) logs a single
  `findphone.location.unavailable` line with `reason=…` and never retries.
  `get_current_fix` then returns `None` for the rest of the process
  lifetime; the responder treats `None` as "send state-only heartbeat".
- Never write raw coordinates to logs or history. The backend's success
  log only emits `accuracy=…`. The responder logs nothing about lat/lng.
  History rows still contain no location data (transfers are file content
  only). Pinned by
  `test_desktop_location_backend.test_get_fix_does_not_log_coordinates`.
- Heartbeat loop: `FindDeviceResponder` now schedules a periodic heartbeat
  via injectable `start_heartbeat`. Real impl uses a daemon thread that
  sleeps on a stop event so cancel returns immediately. Each tick queries
  the location provider and includes `lat`/`lng`/`accuracy` when a fix is
  available; otherwise sends state-only.
- Heartbeat safety: tick verifies the session is still active for the
  captured `sender_id` before sending — late callbacks after `stop()` or
  after the session moved to a different sender are no-ops. Provider
  exceptions are logged and degrade to "no coords this tick".
- Document setup/permission limitations for GeoClue: see the module
  docstring at `desktop/src/backends/linux/location_backend.py`. Headless
  runs without a desktop session, sandboxed AppImages without a
  `xdg-desktop-portal` location proxy, and GNOME's "Location Services"
  toggle (Settings → Privacy) all fall through to the fail-soft path.
- Hardening section:
  - v1 ships best-effort sound + normal-session modal (M.8 wiring,
    `GtkSubprocessAlert`).
  - Lock-screen behavior is intentionally NOT v1. The modal is a normal
    GTK4 `Adw.ApplicationWindow`; whether it surfaces above the GNOME /
    KDE lock screen is compositor- and policy-dependent. The sound loop
    runs in a background thread and is not gated on the session being
    unlocked, so an audible alarm should still fire while locked on most
    Linux distros (PulseAudio runs per-user-session, not per-greeter).
  - Later: test sound while the user session is locked. Test whether the
    normal GTK4 modal appears above the lock screen on GNOME / KDE
    Plasma / Cinnamon. If it does not, evaluate the
    `org.freedesktop.Notifications` urgency=critical path, the
    `xdg-desktop-portal` Background portal, a separate user-systemd
    service that owns the alert process, or a small helper invoked via
    polkit.
  - The responder/sound paths are already independent of the modal so
    "no modal but still ringing" works without code changes.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_find_device_responder`
  passed 2026-04-30 (26 tests; 7 new for M.9 location wiring: initial
  heartbeat with/without fix, periodic heartbeat picks up new fix,
  late-firing heartbeat is no-op, stale-sender callback is no-op,
  provider exception falls back to state-only, payload-coord
  no-log-friendly-repr contract).
- `python3 -m unittest tests.protocol.test_desktop_location_backend`
  passed 2026-04-30 (4 tests: NullLocationProvider always returns None,
  no-gi fallback, geoclue-unreachable fallback, accuracy-only logging).
- `python3 -m unittest tests.protocol.test_desktop_fasttrack_consumer`
  passed 2026-04-30 (consumer tests still pin coord-aware send_update
  shape; fixture now injects `NullLocationProvider` to avoid MagicMock
  auto-vivification of a truthy provider).
- `python3 -m unittest discover -s tests/protocol -p 'test_desktop_*.py'`
  passed 2026-04-30 (445 / 449 — 4 pre-existing PHP integration test
  errors unrelated to this branch).
- `python3 -m py_compile desktop/src/find_device_responder.py
  desktop/src/interfaces/location.py
  desktop/src/backends/linux/location_backend.py
  desktop/src/platform/contract/desktop_platform.py
  desktop/src/platform/linux/compose.py desktop/src/poller.py` passed
  2026-04-30.
- `git diff --check` passed 2026-04-30.
- Manual normal-session locate smoke: not run in this sandbox; lock-screen
  matrix is deferred to the hardening follow-up as planned.

### M.10 - Compatibility, docs, and final verification

Status: completed 2026-04-30

Goal: finish the migration without leaving single-device assumptions behind.

Work:

- User-facing "phone" labels swept where the target is now any connected
  device:
  - `desktop/install.sh`: install banner now describes per-device "Send to
    <device>" scripts appearing after pairing instead of a single generic
    "Send to Phone".
  - `desktop/uninstall.sh`: now removes any Nautilus / Nemo script
    carrying the M.6 managed sentinel
    `desktop-connector:managed-fm-target` plus the legacy "Send to Phone"
    files plus the Dolphin service file.
  - `desktop/README.md`: tagline + first-run paragraph + features bullets
    use "connected device" / per-device send target language; tray status
    legend says "paired device offline" instead of "phone offline".
  - `desktop/packaging/appimage/README.md`: subprocess-windows list now
    includes `pairing` and `locate-alert`; documents that `find-phone`
    and `locate-alert` retain `phone` for IPC stability while user-visible
    labels say "Find my Device" / "Being located".
  - top-level `README.md`: features bullet renamed
    "Send files (phone↔PC)" → "Send files (between any pair of paired
    devices)"; added "Multi-device" bullet; image alt text updated.
  - `desktop/src/main.py` --help docstring, `desktop/src/brand.py` CSS
    comment, and the dead-code tkinter pairing copy in
    `desktop/src/pairing.py:run_pairing_gui`.
  - `desktop/src/windows.py` onboarding subtitle.

- Protocol-compatible names intentionally retained, documented in code
  comments + protocol docs:
  - `phone_id` / `phone_pubkey` JSON fields in pairing handshake (server
    contract).
  - `fn=find-phone` fasttrack payload key (D5; receivers also accept
    `fn=find-device`, senders stay legacy).
  - `findphone.*` event vocabulary (Android symmetry per
    `docs/diagnostics.events.md`).
  - `MessageType.FIND_PHONE_*` enum values (cross-runtime contract).
  - `find-phone` and `locate-alert` GTK4 window subcommand names (AppRun
    `--gtk-window=` IPC, tray's `_open_gtk4_window` dispatch, `windows.py`
    argparse choices).
  - `desktop-connector.find-phone` log namespace.
  - `desktop/nautilus-send-to-phone.py` filename (legacy adoption fingerprint).

- Doc updates:
  - `docs/protocol.examples.md`: Fasttrack section now documents the
    `fn=find-phone` / `fn=find-device` alias rule and the optional
    `lat`/`lng`/`accuracy` heartbeat fields with the privacy contract
    that coordinates never appear in any log record.
  - `docs/protocol.compatibility.md`: two new rows pin the alias as
    extending and the desktop receiver's optional coordinate fields as
    extending.
  - `docs/diagnostics.events.md`: new `device.*` and
    `file_manager.*` event sections (for M.0/M.6); expanded
    `findphone.*` table with every event name introduced by M.7–M.9
    (start/stop accept paths, dropped-concurrent, timeout, alert
    subprocess + sound lifecycle, consumer drop reasons, location
    backend).

- Screenshot refresh deferred. Layouts are now stable, but capturing
  them is a separate manual workstream that needs a real desktop session
  with at least two paired devices to render the new pickers and the
  Settings pair list. Image alt text in `README.md` was updated so the
  rendered hover/screen-reader summary describes the new content even
  while the underlying PNGs are unchanged.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_multi_device_config
  tests.protocol.test_desktop_history_multi_device
  tests.protocol.test_desktop_send_folder_rejection
  tests.protocol.test_desktop_appimage_install_hook
  tests.protocol.test_desktop_message_contract` — all 43 tests passed
  2026-04-30.
- `python3 -m unittest tests.protocol.test_desktop_pairing_naming
  tests.protocol.test_desktop_pairing_window_source
  tests.protocol.test_desktop_settings_multi_device_source
  tests.protocol.test_desktop_find_device_source
  tests.protocol.test_desktop_find_device_responder
  tests.protocol.test_desktop_fasttrack_consumer
  tests.protocol.test_desktop_location_backend
  tests.protocol.test_desktop_file_manager_integration` — all 69 M.5–M.9
  focused tests passed 2026-04-30.
- `python3 -m unittest discover -s tests/protocol -p 'test_desktop_*.py'`
  passed 2026-04-30 (445 / 449 — 4 pre-existing PHP integration test
  errors unrelated to this branch).
- `python3 -m py_compile desktop/src/main.py desktop/src/windows.py
  desktop/src/pairing.py desktop/src/brand.py` passed 2026-04-30.
- `bash -n desktop/install.sh desktop/uninstall.sh
  desktop/install-from-source.sh` passed 2026-04-30.
- `git diff --check` passed 2026-04-30.

Skipped (documented per the plan):

- Manual GTK smoke for the per-device pickers (Send / History / Find /
  Settings) and the "Being located" modal subprocess: not run in this
  sandbox; prior GTK smoke runs in this repo have been
  environment-gated. Source tests already pin the structural
  invariants for each window.
- Lock-screen modal-above-greeter matrix: explicitly deferred per M.9's
  hardening section. The modal + sound paths are independent so an
  audible alert is expected to fire while locked on most Linux
  compositors regardless of whether the modal is visible above the
  greeter.
- Manual desktop-to-desktop locate smoke: requires two desktop clients
  on the same relay; covered at the unit level by the
  dispatcher-round-trip + Poller fasttrack consumer tests.

### M.11 - Desktop-to-desktop pairing bootstrap

Status: completed 2026-04-30

Goal: let two desktops introduce themselves to each other without a
phone or camera in the loop.

Background: M.0 – M.10 added support for N>1 paired peers but kept the
one-way QR-scan bootstrap. Two desktops cannot pair today because
neither typically has a camera. The fix is a "Pair desktop" alternate
mode in the existing pairing window, exchanging a **pairing key** (D9)
through whichever side channel the user has available — copy/paste
text or sneaker-net file.

This chunk is purely a desktop-side feature. The server contract,
crypto handshake, and Android client are all untouched.

UX summary:

- The pairing window keeps phone-pairing as the default mode (QR +
  verification code, current M.5 path). Below the QR there is a new
  "Pair desktop" button that swaps the window into desktop-pair mode.
- Desktop-pair mode shows a "Pair phone" button to swap back, plus
  four pairing-key buttons arranged in two role-symmetric groups:
  - **Share your pairing key** (inviter side) — `Show pairing key`
    (display the text for copy-paste) and `Export pairing key` (save
    a `.dcpair` file).
  - **Use someone else's pairing key** (joiner side) — `Enter pairing
    key` (paste the text) and `Import pairing key` (open a `.dcpair`
    file).
- Both groups are visible together. The window doesn't ask the user
  upfront which role they're playing; they pick by clicking the
  matching button.

Work:

- **New module** `desktop/src/pairing_key.py` (or extension to
  `desktop/src/pairing.py`):
  - `PairingKey` dataclass mirroring the QR JSON shape (`server`,
    `device_id`, `pubkey`, `name`).
  - `encode(key) -> str` produces the canonical D9 string
    `dc-pair:<base64>`.
  - `decode(text) -> PairingKey` runs the D9 parser (whitespace trim,
    optional prefix strip, base64 decode, JSON parse, type checks).
    Raises typed errors: `PairingKeyParseError`, `PairingKeySchemaError`.
  - `validate_for_join(key, *, config, crypto, registry)` enforces D8
    + D10 + already-paired refusal. Raises typed errors:
    `SelfPairError`, `RelayMismatchError`, `AlreadyPairedError`.
  - `default_filename(key) -> str` produces a sanitised
    `<inviter-name>.dcpair` filename for the export dialog.

- **Pairing window restructure** (`windows.py:show_pairing`):
  - Existing pages become `qr` (phone, default), `naming` (shared M.5
    step). Add `desktop` (the new mode hub) and `desktop_join` (paste
    + verification + confirm).
  - QR page: add `Pair desktop` button at the bottom of the existing
    button row. Click → switch to `desktop`.
  - Desktop-mode page (`desktop`):
    - Top button row: `Pair phone` (back to `qr`) + `Cancel`.
    - Two `Adw.PreferencesGroup` sections side-by-side or stacked:
      - **Share your pairing key** with subtitle naming the local
        device (`config.device_name`). Two buttons: `Show pairing
        key`, `Export pairing key`. Both encode the local pairing
        data via `pairing_key.encode(...)` before triggering the
        respective surface (string-display dialog or file save
        dialog).
      - **Use someone else's pairing key** with subtitle "Paste a
        pairing key from another desktop". Two buttons: `Enter
        pairing key`, `Import pairing key`. Both gather the pairing
        key text and route it through `pairing_key.decode(...)` +
        `validate_for_join(...)` before continuing.
    - The desktop-mode page also keeps a live "Waiting for incoming
      pair…" status row that reuses the existing
      `/api/pairing/poll` loop. If a join request arrives while the
      user is on this page (someone elsewhere ran Show + Enter), the
      window switches automatically to the inviter waiting state and
      shows the verification code — same path as the QR page.
  - Joiner page (`desktop_join`):
    - Status: "Pairing with `<inviter-name>` (`<short-id>`)".
    - Verification code displayed prominently (joiner derives it
      immediately on parse).
    - Confirm Pairing + Cancel buttons.
    - On Confirm: switch to `naming`, default name from
      `next_default_device_name`, save via `Config.add_paired_device`,
      `mark_active(reason="paired")`, run
      `sync_file_manager_targets`. Inline guidance:
      "Verify the code matches on the other desktop's screen before
      confirming."

- **Show pairing key dialog** (string surface):
  - `Adw.MessageDialog` with `set_extra_child(GtkTextView)` containing
    the encoded `dc-pair:...` string in a monospace, selectable label.
  - "Copy" button writes to clipboard via
    `LinuxClipboardBackend.write_text`. "Done" closes.
  - Body copy: "Send this key to the other desktop through a channel
    you trust (encrypted chat, password manager, USB stick). Anyone
    with the key can request to pair with this desktop while the
    pairing window is open."

- **Export pairing key dialog** (file surface):
  - `LinuxDialogBackend.save_file` (or the equivalent GTK4
    `Gtk.FileDialog.save`) with default name from
    `pairing_key.default_filename(...)`.
  - Writes the encoded `dc-pair:...` string to the chosen path
    (single line, UTF-8, no trailing newline). 0o600 perms — same
    bucket as identity material.
  - Surface a toast "Pairing key exported to <path>" on success or
    show the error on failure.

- **Enter pairing key dialog** (string surface):
  - `Adw.MessageDialog` with `set_extra_child` containing a
    `GtkTextView` for paste (multi-line tolerant, parser strips
    whitespace).
  - "Continue" runs decode + validate. Errors render inline beneath
    the text view; success switches the window to the joiner page.

- **Import pairing key dialog** (file surface):
  - `LinuxDialogBackend.pick_files` (single-file mode, filtered to
    `.dcpair`).
  - Reads the file, runs decode + validate. Errors render in a toast;
    success switches the window to the joiner page.

- **Validation rules** (D8 + D10 + already-paired) — implemented once
  in `pairing_key.validate_for_join` and reused by both joiner
  surfaces. Error messages are user-facing:
  - `SelfPairError`: "This pairing key is from this same desktop."
  - `RelayMismatchError`: "This pairing key is for a different relay
    server (`<other>`). Both desktops must be configured for the same
    relay before pairing."
  - `AlreadyPairedError`: "You're already paired with `<name>`. Unpair
    first if you want to re-pair."

- **Wire call**: identical to the existing phone-side path —
  `api.send_pairing_request(target_device_id=key.device_id,
  phone_pubkey=crypto.get_public_key_b64())`. Field name retained per
  D10.

- **Symkey derivation + verification code**: identical helpers as the
  current pairing window. Joiner displays the verification code
  immediately on validate-success because both pubkeys are in hand.

- **No server-side confirm signal needed** on the joiner side. Joiner's
  Confirm persists locally. If the inviter never clicks Confirm
  on their end, the server-side `pairings` row never lands and
  transfers between the two devices return 403 — existing
  auth-recovery banner surfaces "Re-pair X". The joiner page's
  inline help mentions this so a stuck pair is debuggable.

- **Settings entry-point copy**: the M.10 follow-up "Pair another
  device" row already opens the pairing window; with M.11 it lands on
  the QR page by default and the user clicks "Pair desktop" if they
  want desktop-mode. Tray menu's "Pair another device..." likewise.
  No tray changes needed.

- **Diagnostics events** (`docs/diagnostics.events.md` `pairing`
  section, all desktop-only):
  - `pairing.key.shown` info — user clicked Show pairing key.
  - `pairing.key.exported` info `path` — user clicked Export and a
    file was written. Path is logged (it's user-chosen and not a
    secret); the key string itself is never logged.
  - `pairing.key.import_parse_failed` warning `surface` (`text` |
    `file`) — D9 parser rejected. No payload contents.
  - `pairing.key.import_relay_mismatched` warning `local` `remote` —
    relay URL hostnames only (paths/queries trimmed); never the full
    URL since it can include subdirectory tokens.
  - `pairing.key.import_self_pair_refused` warning — no fields.
  - `pairing.key.import_already_paired_refused` warning `peer` —
    short-id only.
  - `pairing.request.sent_as_joiner` info `target` — short-id of
    the inviter we just sent a pairing request to.

  Never log: the encoded key, decoded key contents, symkey, verification
  code.

Verification:

- Unit tests for `pairing_key.encode/decode` round-trip
  (`test_desktop_pairing_key_codec`):
  - Encode → decode round-trips a fixture key byte-for-byte.
  - Encoded form starts with `dc-pair:`, contains only URL-safe
    base64 characters.
  - Decode tolerates leading/trailing whitespace, embedded newlines
    (e.g. soft-wrapped chat paste).
  - Decode tolerates the prefix being absent (forgiving paste UX).
  - Decode rejects invalid base64, malformed JSON, missing required
    fields, wrong-typed fields, empty `name`.

- Unit tests for `pairing_key.validate_for_join`
  (`test_desktop_pairing_key_validate`):
  - Self-pair (`device_id == crypto.get_device_id()`) → `SelfPairError`.
  - Relay mismatch (different scheme, different host, trailing-slash
    differences treated as equal) → `RelayMismatchError`.
  - Already-paired device id → `AlreadyPairedError`.

- Functional test for the joiner happy path
  (`test_desktop_pairing_key_join_flow`):
  - Real `Config` + `KeyManager`, fake `ApiClient` with
    `send_pairing_request → True`, a generated peer pubkey + matching
    relay URL.
  - Build a key with `pairing_key.encode(...)`, pipe through decode +
    validate + a `join_via_pairing_key(...)` helper that mirrors what
    the GTK joiner path does without GTK.
  - Assert: pair persists with the chosen name (M.5 naming step
    applied), `active_device_id` flipped, `api.send_pairing_request`
    called once with expected args, file-manager sync invoked,
    derived symkey matches what `crypto.derive_shared_key` would
    return on the inviter side using the joiner's pubkey.

- Source tests for the pairing-window restructure
  (`test_desktop_pairing_window_source`, extended):
  - `qr` page contains a `Pair desktop` button.
  - `desktop` page exists and contains all four key buttons by exact
    label: `Show pairing key`, `Export pairing key`, `Enter pairing
    key`, `Import pairing key`, plus a `Pair phone` swap-back button.
  - `desktop_join` page contains the Confirm Pairing + Cancel
    buttons and a verification-code display.
  - Naming page (`naming`) is shared and reachable from both `qr` →
    confirm and `desktop_join` → confirm.

- Source tests for the diagnostics-event vocabulary additions
  (`test_desktop_pairing_key_events_source`):
  - The new event names appear at their emit sites.
  - The encoded key string never appears as a `%s` argument to a
    log statement (grep-style negative assertion).

- Manual smoke (deferred, document if skipped):
  - Two desktops on the same relay. Pair via Show + Enter (string
    channel) and again via Export + Import (file channel) in both
    directions. Send a file each way to exercise the symkey. Run a
    Find my Device session each way to exercise M.8 with a
    desktop-as-target peer. Lock the inviter's screen between
    receiving the pairing request and clicking Confirm to confirm
    the auth-recovery banner picks up the failure correctly.

Verification (executed 2026-04-30):

- `python3 -m unittest tests.protocol.test_desktop_pairing_key_codec`
  — 16 tests passed. Covers encode/decode round-trip, URL-safe base64
  invariants, whitespace + newline tolerance, prefix-optional + extra-padding
  tolerance, and refusal of malformed / non-object / missing-field /
  wrong-typed / empty-required-field inputs.
- `python3 -m unittest tests.protocol.test_desktop_pairing_key_validate`
  — 11 tests passed. Self-pair, relay mismatch (different host /
  different path / case-insensitive scheme + host / trailing slash
  tolerated), already-paired refusal, and explicit-registry-injection
  paths plus URL normalization unit tests.
- `python3 -m unittest tests.protocol.test_desktop_pairing_key_join_flow`
  — 4 tests passed. Includes the critical ECDH-symmetry assertion:
  the joiner's derived symkey byte-for-byte equals what the inviter
  would derive on its side from the joiner's pubkey. Send-failure
  path raises `JoinRequestError`; on-synced exception is swallowed
  so a file-manager sync failure can't strand a half-saved pair.
- `python3 -m unittest tests.protocol.test_desktop_pairing_window_source`
  — 9 tests passed. Pins the four pairing-key buttons (Show / Export
  / Enter / Import) by exact label, the desktop-mode hub, the
  joiner verification page, the role-branched naming-step save, the
  Pair-desktop / Pair-phone toggles, the auto-switch back to QR on
  incoming pair, and the export-file 0o600 perms invariant.
- `python3 -m unittest tests.protocol.test_desktop_pairing_key_events_source`
  — 3 tests passed. Pins the eight `pairing.key.*` event names plus
  the negative-grep contract that the encoded key string,
  `shared_key`, and `verification_code` never appear as `%s`
  arguments in any log statement.
- `python3 -m unittest discover -s tests/protocol -p 'test_desktop_*.py'`
  — 486 / 490 (4 pre-existing PHP integration errors unrelated).
- `python3 -m py_compile desktop/src/pairing_key.py
  desktop/src/windows.py desktop/src/dialogs.py
  desktop/src/interfaces/dialogs.py
  desktop/src/backends/linux/dialog_backend.py` — all clean.
- `git diff --check` — clean.

Out of scope (explicitly):

- Three-or-more desktop chains. M.11 covers a single pair operation;
  N pairings between N desktops is just N-1 invocations of M.11.
- Pairing across different relays. D8 forbids it. A future plan can
  add a "share-relay-config first, then pair" flow if there's demand.
- QR-decode on the joiner desktop for cases where someone takes a
  photo of the inviter's screen. Out of scope for v1; the
  text/file flow covers the same threat model with less complexity
  and no camera dependency.
- Re-pair shortcut from the auth-recovery banner. The banner already
  drops the user on the existing pairing window; with M.11 they
  click "Pair desktop" there.
- Encrypted pairing-key files (passphrase-protected `.dcpair`). The
  key is paste-secret material in the same bucket as the QR image;
  layering passphrase encryption on top is a separate hardening
  concern (M.12+) if real-world misuse warrants it.

## Finalized Decisions

1. History clearing is scoped to the selected device's visible history.
2. Settings includes per-device rename, matching Android.
3. Device names must be unique. Duplicate saves are rejected; existing duplicate
   state is normalized by appending a short device id and saving the corrected
   name.
4. `Find my device` preselects the active device. The user can choose another
   target before starting. Once locating is in progress, the selector is
   disabled until the session stops.
5. Lock-screen support is a later hardening concern. v1 ships best-effort sound
   and normal-session modal behavior.
6. Desktop-to-desktop pairing requires both desktops to already be configured
   for the same relay. The joiner refuses to silently switch relays on a
   mismatch (D8).
7. The pairing key is `dc-pair:<base64>` of the existing QR JSON shape (D9).
   Same content travels through two interchangeable channels: string (Show /
   Enter) and file (Export / Import via `.dcpair`). Both paths land on the
   same parser. The pairing key is paste-secret material in the same
   threat-model bucket as the QR image.
8. The wire field name `phone_pubkey` on `/api/pairing/request` is retained
   as "requester pubkey by function" (D10). The desktop-as-requester case
   does not introduce a new endpoint or rename existing fields.
