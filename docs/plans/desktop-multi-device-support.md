# Desktop multi-device support

Status: open. Implementation has not started.

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

Status: pending

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
- Sync generated file-manager labels after rename.

Verification:

- Unit tests for pairing save/name/default-name behavior.
- Source tests for settings rendering all pairs, not first pair.

### M.6 - File-manager integration per connected device

Status: pending

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

- Extend `test_desktop_appimage_install_hook.py` and
  `test_desktop_send_folder_rejection.py` for per-device rendered scripts.
- Add tests proving unpair removes only the matching managed script and never
  deletes unmarked user files.

### M.7 - Find my device sender UI

Status: pending

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

Verification:

- Existing fasttrack contract tests still pass.
- Manual send-to-Android find-device smoke.

### M.8 - Desktop find-device receiver

Status: pending

Goal: allow this desktop app to be located by another connected device.

Work:

- Add background fasttrack polling to `Poller` without blocking file transfer
  polling.
- Decrypt each fasttrack message with the sender's paired symmetric key.
- Route payloads through `FasttrackAdapter` and `MessageDispatcher`.
- Add a `FindDeviceResponder` service for `start`, `stop`, timeout, state
  updates, and concurrency rules.
- For non-silent locate requests, play an alert sound and show an always-on-top
  best-effort GTK modal with stop action and sender/device information.
- For silent locate requests, skip modal and sound.
- Send encrypted status/location updates back to the requesting device and ACK
  processed fasttrack messages.

Verification:

- Unit tests for adapter dispatch, unknown sender handling, concurrent start
  drop, stop, timeout, and encrypted response shape.
- Manual desktop-to-desktop smoke if two desktop clients are available.

### M.9 - Desktop location provider and lock-screen hardening

Status: pending

Goal: provide best-effort desktop location now and document lock-screen support
as a later hardening concern.

Work:

- Add a location provider abstraction under `desktop/src/platform`.
- Linux first pass: try GeoClue over D-Bus if available and permitted.
- Fallback: no coordinates, heartbeat only.
- Never write raw coordinates to logs or history.
- Document setup/permission limitations for GeoClue.
- Hardening section:
  - Do not block v1 on lock-screen UI support.
  - Document that sound plus the normal-session modal are the v1 behavior.
  - Later, test sound while the user session is locked.
  - Later, test whether normal GTK windows can appear over GNOME/KDE lock
    screens.
  - If normal windows cannot appear, evaluate notification urgency, portal
    APIs, systemd user services, or a separate helper.

Verification:

- Unit tests for provider fallback and coordinate redaction.
- Manual normal-session locate smoke; lock-screen matrix is deferred to the
  hardening follow-up.

### M.10 - Compatibility, docs, and final verification

Status: pending

Goal: finish the migration without leaving single-device assumptions behind.

Work:

- Search and resolve remaining user-facing "phone" labels where the target is
  now a connected device.
- Keep protocol-compatible names documented where they intentionally remain.
- Update `docs/protocol.examples.md`, `docs/protocol.compatibility.md`, and
  `docs/diagnostics.events.md` for new events/fields.
- Update README/desktop README references from "Send to Phone" and "Find my
  Phone" where appropriate.
- Add or update screenshots only after UI behavior is stable.
- Run focused desktop unit tests and any feasible integration checks.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_multi_device_config`
- `python3 -m unittest tests.protocol.test_desktop_history_multi_device`
- `python3 -m unittest tests.protocol.test_desktop_send_folder_rejection`
- `python3 -m unittest tests.protocol.test_desktop_appimage_install_hook`
- `python3 -m unittest tests.protocol.test_desktop_message_contract`
- Existing streaming and receive-action tests touched by the migration.
- Document any skipped GTK or lock-screen checks with exact reason.

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
