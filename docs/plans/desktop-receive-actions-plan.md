# Desktop Receive Actions — implementation plan

## Goal

Add configurable automated actions for items received on the desktop app.

Current desktop behavior should remain the default where possible: received files are saved as they are today, and URL handling keeps the existing “open URL” behavior unless the user changes the setting.

This feature is intentionally desktop-only for the first implementation. It should not require server changes or Android protocol changes.

## Product behavior

Add a new settings section named **Receive Actions**.

The user can choose what the desktop app should do after receiving an item:

| Item type | Actions |
|---|---|
| URL | Open in default browser / Copy to clipboard / No action |
| Text | Copy to clipboard / No action |
| Image | Open in default image viewer / No action |
| Video | Open in default video viewer / No action |
| Document | Open in default document viewer / No action |

Default values:

```json
{
  "receive_actions": {
    "url": "open",
    "text": "copy",
    "image": "none",
    "video": "none",
    "document": "none"
  }
}
```

Reasoning:

- `url: open` preserves the current auto-open URL behavior.
- `text: copy` preserves the current behavior for received non-URL text.
- Files default to `none`, so they are only saved as they are today.
- “No action” means: save normally, update history normally, show existing notifications normally, but do not auto-open or copy.

## Settings placement

Use the existing Settings window. Do not introduce tabs yet.

The new order should be:

```text
Settings
├─ Connection
├─ Receive Actions
├─ This Device
├─ Paired Device
├─ Connection Statistics
└─ Logs
```

Important: **Logs must be the last section.**

Rationale:

- Receive Actions are normal user behavior settings, not diagnostics.
- Logs are diagnostic/maintenance controls and should stay at the bottom.
- The current `Auto-open links` switch belongs conceptually under Receive Actions, not Connection.
- Tabs are not needed yet; a single scrollable libadwaita preferences page remains enough.

## UI design

Use `Adw.PreferencesGroup` for the new section.

Use `Adw.ComboRow`, not checkboxes.

Reason: the options are mutually exclusive. For example, URL action should be one selected mode, not multiple checkboxes.

Suggested UI:

```text
Receive Actions

URL
[ Open in default browser  ▾ ]

Text
[ Copy to clipboard        ▾ ]

Image
[ No action                ▾ ]

Video
[ No action                ▾ ]

Document
[ No action                ▾ ]
```

Suggested subtitles:

```text
URL
What to do when received text is detected as a URL.

Text
What to do after receiving text that is not only a URL.

Image
What to do after receiving an image file.

Video
What to do after receiving a video file.

Document
What to do after receiving a document file.
```

Suggested user-facing labels:

```text
Open in default browser
Copy to clipboard
No action

Copy to clipboard
No action

Open in default image viewer
Open in default video viewer
Open in default document viewer
No action
```

Implementation note: `Adw.ComboRow` is better than switches because it scales if new actions are added later, for example “Ask every time”, “Reveal in folder”, or “Open with custom app”.

## Configuration model

Add a structured config property to `desktop/src/config.py`.

New canonical config key:

```json
{
  "receive_actions": {
    "url": "open",
    "text": "copy",
    "image": "none",
    "video": "none",
    "document": "none"
  }
}
```

Allowed internal values:

```text
url:      open | copy | none
text:     copy | none
image:    open | none
video:    open | none
document: open | none
```

Suggested Python constants:

```python
RECEIVE_ACTION_OPEN = "open"
RECEIVE_ACTION_COPY = "copy"
RECEIVE_ACTION_NONE = "none"

DEFAULT_RECEIVE_ACTIONS = {
    "url": RECEIVE_ACTION_OPEN,
    "text": RECEIVE_ACTION_COPY,
    "image": RECEIVE_ACTION_NONE,
    "video": RECEIVE_ACTION_NONE,
    "document": RECEIVE_ACTION_NONE,
}
```

Add a property:

```python
@property
def receive_actions(self) -> dict:
    actions = dict(DEFAULT_RECEIVE_ACTIONS)
    stored = self._data.get("receive_actions", {})

    if isinstance(stored, dict):
        for kind, action in stored.items():
            if kind in actions and action in allowed_actions_for(kind):
                actions[kind] = action

    return actions

@receive_actions.setter
def receive_actions(self, value: dict) -> None:
    normalized = dict(DEFAULT_RECEIVE_ACTIONS)

    for kind, action in value.items():
        if kind in normalized and action in allowed_actions_for(kind):
            normalized[kind] = action

    self._data["receive_actions"] = normalized
    self.save()
```

Also add convenience helpers:

```python
def get_receive_action(self, kind: str) -> str:
    return self.receive_actions.get(kind, DEFAULT_RECEIVE_ACTIONS.get(kind, "none"))

def set_receive_action(self, kind: str, action: str) -> None:
    actions = self.receive_actions
    actions[kind] = action
    self.receive_actions = actions
```

## Backwards compatibility

The current desktop config has `auto_open_links`.

Do not remove it immediately.

Migration behavior:

```text
If receive_actions is missing:
    receive_actions.url = "open" if auto_open_links is true/missing
    receive_actions.url = "none" if auto_open_links is false
    receive_actions.text = "copy"
    image/video/document = "none"
```

Then write the new `receive_actions` object into `config.json`.

Keep the old `auto_open_links` property for one or two releases, but stop using it from the UI.

Possible deprecation path:

1. Release A:
   - Add `receive_actions`.
   - Migrate from `auto_open_links`.
   - Hide/remove the old `Auto-open links` switch from Connection.
   - Leave `auto_open_links` property in code for compatibility.

2. Release B:
   - Remove reads/writes of `auto_open_links` if no longer needed.

## MIME/type classification

Create a small desktop-side classifier.

Suggested function:

```python
def classify_received_item(path: Path | None, text: str | None = None) -> str | None:
    ...
```

Return values:

```text
url
image
video
document
other
```

### URL detection

For text payloads:

- Trim whitespace.
- Treat as URL-only only if the entire text is a single URL.
- If text contains a URL plus other text, run URL action processing for
  the detected URL and text action processing for the full text.
- Accept `http://` and `https://`.
- Optionally accept common schemes later: `mailto:`, `tel:`, `geo:`.
- Avoid auto-opening arbitrary shell-like strings or partial text.

Suggested initial rule:

```python
parsed = urllib.parse.urlparse(text.strip())
is_url = parsed.scheme in ("http", "https") and bool(parsed.netloc)
```

### File detection

Prefer MIME detection over extension-only detection.

Possible order:

1. `mimetypes.guess_type(path.name)`
2. fallback to extension list
3. fallback to `other`

Suggested MIME families:

```text
image:    image/*
video:    video/*
document: application/pdf
          text/plain
          text/markdown
          application/msword
          application/vnd.openxmlformats-officedocument.wordprocessingml.document
          application/vnd.oasis.opendocument.text
          application/rtf
          application/vnd.ms-excel
          application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
          application/vnd.oasis.opendocument.spreadsheet
          application/vnd.ms-powerpoint
          application/vnd.openxmlformats-officedocument.presentationml.presentation
          application/vnd.oasis.opendocument.presentation
```

Do not treat archives as documents in v1:

```text
zip
rar
7z
tar.gz
```

Reason: auto-opening archives can be surprising and less useful.

## Action execution

### Open action

Opening should behave like clicking the file in the file manager.

On Linux, use:

```python
subprocess.Popen(["xdg-open", str(path)], ...)
```

For URLs:

```python
subprocess.Popen(["xdg-open", url], ...)
```

Rules:

- Do not use `shell=True`.
- Do not block the receiver loop.
- Do not fail the transfer if opening fails.
- If opening fails, log a warning only when logging is enabled.
- Do not mark the transfer as failed just because the post-receive action failed.

### Copy action

For URL copy action:

- Copy the URL to the desktop clipboard.
- Reuse the existing clipboard helper if possible.
- Only URL gets `copy` in v1.
- Do not add “copy image to clipboard” yet unless it is already easy and reliable.

## Receive flow integration

The action should run after the item is fully received and saved, not before.

Desired order for files:

```text
1. Download/decrypt received item.
2. Save it to the current save directory.
3. Add/update transfer history.
4. Classify item.
5. Read configured receive action.
6. Run action if action != none.
7. Show/update notification/history as usual.
```

Desired order for URL/text:

```text
1. Receive/decrypt text payload.
2. Detect whether the whole payload is a single URL.
3. If the whole payload is a single URL:
   - apply URL receive action only.
4. If the payload is not only a URL:
   - apply URL receive action if an embedded URL exists.
   - apply text receive action to the full text.
5. Stage any clipboard-producing action in memory.
6. If a staged clipboard value exists, write it to the real clipboard once.
```

For embedded URL text, run the URL action first and the Text action
second. This means `url=copy` plus `text=copy` produces one clipboard
write containing the full received text.

Do not change server protocol for v1.

## Existing Auto-open URL behavior

The current auto-open URL behavior should become the URL row inside Receive Actions.

Mapping:

```text
auto_open_links = true  → receive_actions.url = "open"
auto_open_links = false → receive_actions.url = "none"
```

If current URL behavior also shows an Open/Copy dialog, keep that behavior only for the setting that maps to the current UX. If the new setting is `copy`, copy directly. If it is `none`, do not open automatically.

If the current behavior is more nuanced than the name suggests, preserve the safest interpretation:

```text
url = open → current default URL behavior
url = copy → copy to clipboard
url = none → do nothing beyond saving/history/notification
text = copy → copy full received text to clipboard
text = none → do not copy received text
```

## Error handling

Post-receive actions must be best-effort.

Examples:

```text
xdg-open missing
no default app configured
file deleted before opening
clipboard unavailable
Wayland/X11 clipboard backend error
```

Handling:

- Do not crash the app.
- Do not retry forever.
- Do not mark the transfer failed.
- Optional: show a desktop notification “Received file saved, but could not open it.”
- Log detailed error only if logging is enabled.

## Privacy and safety

V1 should not include arbitrary shell commands.

Explicitly avoid:

```text
Run custom command
Run script
Open with shell command
Use user-provided command template
```

Reason: this feature runs automatically on received input. Custom commands would make it easy to create dangerous behavior accidentally.

Safe built-in actions only:

```text
open via xdg-open
copy URL to clipboard
none
```

## Files to modify

Expected files:

```text
desktop/src/config.py
desktop/src/receive_actions.py
desktop/src/windows.py
desktop/src/poller.py
desktop/src/interfaces/shell.py
desktop/src/backends/linux/shell_backend.py
desktop/README.md
tests/protocol/test_desktop_receive_actions_config.py
tests/protocol/test_desktop_receive_actions.py
tests/protocol/test_desktop_receive_actions_poller.py
tests/protocol/test_platform_contract.py
```

Expected changes:

### `desktop/src/config.py`

- Add default receive actions.
- Add getter/setter/helper methods.
- Add migration from `auto_open_links`.
- Keep `auto_open_links` property temporarily.
- Run normalization during `Config.__init__` so callers always see a
  canonical mapping.
- Save the config when migration or normalization changes stored data.

### `desktop/src/receive_actions.py`

- Add URL and file classification helpers.
- Add a small executor that maps `(kind, action)` to safe built-in work.
- Add a text executor that can run URL and Text actions for one payload.
- Use `platform.shell.open_url()` for URLs.
- Use `platform.shell.open_path()` for files.
- Stage clipboard-producing URL/Text actions in memory, then call
  `platform.clipboard.write_text()` at most once.
- Return a boolean or small result object for tests/logging; never raise
  into the receive loop.

### `desktop/src/interfaces/shell.py` and Linux backend

- Add `open_path(path: Path) -> bool`.
- Implement it with `subprocess.Popen(["xdg-open", str(path)])`.
- Keep `open_folder()` unchanged for save-folder behavior.

### `desktop/src/windows.py`

- Remove the old `Auto-open links` switch from Connection.
- Add `Receive Actions` group after Connection.
- Add ComboRows for URL/Image/Video/Document.
- Move Logs group to the bottom of Settings.
- Ensure Settings order stays:
  - Connection
  - Receive Actions
  - This Device
  - Paired Device
  - Connection Statistics
  - Logs

### Receiver code

- In `desktop/src/poller.py`, call the helper from both classic and
  streaming file receive paths after history is updated to `complete`
  and before file-received callbacks fire.
- In `_handle_message_clipboard_text`, route text payloads through the
  receive action helper. Exact single-URL payloads use only
  `receive_actions.url`; text with an embedded URL uses both URL and
  Text actions.
- Keep open actions fire-and-forget. URL copy can use the existing
  bounded clipboard helper.
- Do not apply file actions to `.fn.*` command transfers or clipboard
  image mirroring in v1.

## Execution plan

Parts:

```text
P.0 Baseline and current-behavior check
P.1 Config model and migration
P.2 Receive action helper
P.3 Platform shell open-path support
P.4 Settings UI
P.5 URL and text receive integration
P.6 File receive integration
P.7 Documentation and verification
```

### P.0 - Baseline

Status: completed 2026-04-26.

Baseline result:

```text
python is not installed in this environment; use python3 for local tests.
python3 -m unittest tests.protocol.test_platform_contract: passed, 6 tests.
python3 -m unittest tests.protocol.test_desktop_streaming_recipient: passed, 17 tests.
Current settings order: Connection, Logs, This Device, Paired Device, Connection Statistics.
Current URL behavior: clipboard text is written first, then regex-detected URLs open when auto_open_links is true.
```

- Run the focused existing tests before editing receiver behavior:

```bash
python -m unittest tests.protocol.test_platform_contract
python -m unittest tests.protocol.test_desktop_streaming_recipient
```

- Confirm current settings order in `desktop/src/windows.py`:
  Connection, Logs, This Device, Paired Device, Connection Statistics.
- Confirm current URL behavior in `desktop/src/poller.py`:
  clipboard text is written first, then any regex-detected URL is opened
  when `auto_open_links` is true.

Exit criteria:

```text
Existing focused tests pass, or any pre-existing failure is recorded.
```

### P.1 - Config model and migration

Status: completed 2026-04-26.

Result:

```text
Added receive action constants, defaults, kind-specific validation, migration, accessors, and reload normalization.
Added text receive action defaulting to copy.
Added tests/protocol/test_desktop_receive_actions_config.py.
python3 -m unittest tests.protocol.test_desktop_receive_actions_config: passed, 11 tests.
python3 -m unittest tests.protocol.test_platform_contract tests.protocol.test_desktop_streaming_recipient: passed, 23 tests.
python3 -m unittest tests.protocol.test_desktop_appimage_onboarding tests.protocol.test_desktop_appimage_install_hook tests.protocol.test_desktop_appimage_migration: passed, 48 tests.
```

Implementation:

- Add constants:

```python
RECEIVE_ACTION_OPEN = "open"
RECEIVE_ACTION_COPY = "copy"
RECEIVE_ACTION_NONE = "none"
RECEIVE_KIND_URL = "url"
RECEIVE_KIND_TEXT = "text"
RECEIVE_KIND_IMAGE = "image"
RECEIVE_KIND_VIDEO = "video"
RECEIVE_KIND_DOCUMENT = "document"
DEFAULT_RECEIVE_ACTIONS = {...}
```

- Add `allowed_receive_actions(kind: str) -> set[str]`.
- Add `_normalize_receive_actions(value: object) -> dict[str, str]`.
- Add `_migrate_receive_actions()` called from `Config.__init__` after
  `_load()`.
- Migration rules:
  - If `receive_actions` is missing, seed defaults and map
    `auto_open_links=false` to `url=none`.
  - If `receive_actions` is present but partial or invalid, normalize it
    with defaults.
  - Keep `auto_open_links` readable/writable for compatibility.
- Add:

```python
@property
def receive_actions(self) -> dict[str, str]: ...

@receive_actions.setter
def receive_actions(self, value: dict[str, str]) -> None: ...

def get_receive_action(self, kind: str) -> str: ...
def set_receive_action(self, kind: str, action: str) -> None: ...
```

Tests:

- Add `tests/protocol/test_desktop_receive_actions_config.py`.
- Cover empty config, `auto_open_links=true`, `auto_open_links=false`,
  partial mappings, invalid actions, unknown item types, and setter
  persistence.

Acceptance criteria:

```text
Config.receive_actions always returns all five keys.
Legacy auto_open_links=false maps URL action to none.
Invalid stored values do not leak into runtime behavior.
Existing auto_open_links property still works.
```

### P.2 - Receive action helper

Status: completed 2026-04-26.

Result:

```text
Added desktop/src/receive_actions.py with exact URL detection, embedded URL extraction, text action orchestration, file classification, and best-effort action execution.
Clipboard-producing URL/Text actions are staged and flushed to the real clipboard at most once per received text payload.
Added tests/protocol/test_desktop_receive_actions.py.
python3 -m unittest tests.protocol.test_desktop_receive_actions: passed, 26 tests.
python3 -m unittest tests.protocol.test_desktop_receive_actions_config: passed, 11 tests.
python3 -m unittest tests.protocol.test_platform_contract tests.protocol.test_desktop_streaming_recipient: passed, 23 tests.
```

Implementation:

- Add `desktop/src/receive_actions.py`.
- Implement:

```python
def classify_received_text(text: str) -> tuple[str | None, str | None]:
    ...

def extract_received_urls(text: str) -> list[str]:
    ...

def classify_received_file(path: Path) -> str:
    ...

def apply_receive_action(config, platform, kind: str, *, url: str | None = None,
                         text: str | None = None,
                         path: Path | None = None) -> bool:
    ...

def apply_receive_text_actions(config, platform, text: str) -> bool:
    ...
```

- URL classification must trim whitespace and require the whole payload
  to be one `http://` or `https://` URL using `urllib.parse.urlparse`.
- File classification should prefer `mimetypes.guess_type(path.name)`,
  then extension fallback, then `other`.
- `apply_receive_action()` behavior:
  - `none`: return true without side effects.
  - `url/open`: call `platform.shell.open_url(url)`.
  - `url/copy`: stage `url` as the pending clipboard value.
  - `text/copy`: stage the full received text as the pending clipboard value.
  - `image|video|document/open`: call `platform.shell.open_path(path)`.
  - Unsupported combinations: log and return false.
- `apply_receive_text_actions()` behavior:
  - exact single URL: URL action only.
  - text with embedded URL: URL action for the first detected URL, then
    Text action for the full text.
  - plain text: Text action only.
  - flush pending clipboard once after all selected actions have run.
- Catch exceptions inside the helper. The receive loop must never fail
  because a post-receive action failed.

Tests:

- Add `tests/protocol/test_desktop_receive_actions.py`.
- Use fake config/platform objects; do not call real `xdg-open` or real
  clipboard tools.
- Cover exact URL detection, embedded URL extraction, staged clipboard
  flushing, MIME families, archive rejection, and each action branch.

Acceptance criteria:

```text
URL-only helper accepts only whole http/https payloads.
Embedded URL helper extracts http/https URLs inside text.
Image/video/document helpers classify common MIME types.
Archive and unknown files classify as other.
Action helper is best-effort and side-effect-free for none/other.
```

### P.3 - Platform shell open-path support

Status: completed 2026-04-26.

Result:

```text
Added ShellBackend.open_path(path).
Implemented LinuxShellBackend.open_path() with xdg-open.
Updated platform contract test to assert shell.open_path exists.
python3 -m unittest tests.protocol.test_platform_contract: passed, 6 tests.
python3 -m unittest tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_config: passed, 37 tests.
python3 -m unittest tests.protocol.test_desktop_streaming_recipient: passed, 17 tests.
```

Implementation:

- Extend `ShellBackend` protocol with `open_path(path: Path) -> bool`.
- Implement `LinuxShellBackend.open_path()` with `xdg-open`.
- Leave `open_url()` and `open_folder()` unchanged.
- Update platform contract tests to assert the new method exists.

Tests:

- Update `tests/protocol/test_platform_contract.py`.
- Unit test can assert method shape only; do not spawn external apps.

Acceptance criteria:

```text
Linux platform exposes shell.open_path.
Existing shell.open_url/open_folder behavior remains available.
Platform contract tests pass.
```

### P.4 - Settings UI

Status: completed 2026-04-26.

Result:

```text
Removed the old Auto-open links switch from Connection.
Added Receive Actions ComboRows for URL, Text, Image, Video, and Document.
Moved Logs to the final PreferencesGroup position before the footer labels.
Added tests/protocol/test_desktop_receive_actions_settings.py.
python3 -m unittest tests.protocol.test_desktop_receive_actions_settings: passed, 3 tests.
python3 -m unittest tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_config tests.protocol.test_platform_contract: passed, 43 tests.
python3 -m unittest tests.protocol.test_desktop_streaming_recipient: passed, 17 tests.
Interactive settings smoke was not possible in this sandbox: GTK could not initialize under Xvfb.
```

Implementation:

- In `show_settings()`, remove the `Auto-open links` switch from the
  Connection group.
- Add a `Receive Actions` `Adw.PreferencesGroup` immediately after
  Connection.
- Add one ComboRow per kind:
  - URL: Open in default browser, Copy to clipboard, No action.
  - Text: Copy to clipboard, No action.
  - Image: Open in default image viewer, No action.
  - Video: Open in default video viewer, No action.
  - Document: Open in default document viewer, No action.
- Keep a local label/action mapping so row labels are user-facing and
  config values remain stable strings.
- On `notify::selected`, call `config.set_receive_action(kind, action)`.
- Move Logs group creation so it is appended after Connection
  Statistics. Footer labels may remain after Logs because they are not a
  preferences section.

Manual checks:

```text
Settings shows Receive Actions after Connection.
ComboRows show migrated/default values.
Changing a ComboRow updates config.json.
Closing and reopening Settings preserves selections.
Logs is the final PreferencesGroup.
```

Acceptance criteria:

```text
The old Auto-open links switch is no longer visible.
URL default is Open in default browser.
Text default is Copy to clipboard.
File defaults are No action.
Settings section order matches this plan.
```

### P.5 - URL and text receive integration

Status: completed 2026-04-26.

Result:

```text
Replaced direct clipboard write and regex auto-open in Poller._handle_message_clipboard_text().
Text payloads now route through apply_receive_text_actions().
Exact URL payloads run URL action only.
Embedded URL payloads run URL action and Text action, with at most one real clipboard write.
Plain text payloads run Text action.
Added tests/protocol/test_desktop_receive_actions_poller.py.
python3 -m unittest tests.protocol.test_desktop_receive_actions_poller tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_config tests.protocol.test_desktop_receive_actions_settings tests.protocol.test_platform_contract tests.protocol.test_desktop_streaming_recipient: passed, 71 tests.
```

Implementation:

- In `_handle_message_clipboard_text`, replace direct clipboard write
  and regex-based auto-open with `apply_receive_text_actions()`.
- For exact URL payloads:
  - Add the received history entry.
  - Show the existing notification.
  - Apply `receive_actions.url`.
  - Do not apply `receive_actions.text`.
- For text containing a URL plus other text:
  - Add the received history entry.
  - Show the existing notification.
  - Apply `receive_actions.url` for the detected URL.
  - Apply `receive_actions.text` for the full text.
- For text without a URL:
  - Apply `receive_actions.text` for the full text.
- Clipboard-producing actions must stage a pending value and write the
  real clipboard at most once.
- Log helper failures, but do not return early after history/notification
  are recorded.

Tests:

- Add `tests/protocol/test_desktop_receive_actions_poller.py`.
- Build a Poller with fake config/platform/history.
- Cover:
  - `url=open` calls `open_url` and not `write_text`.
  - `url=copy` calls `write_text` and not `open_url`.
  - `url=none` calls neither.
  - `text=copy` copies plain text.
  - `text=none` does not copy plain text.
  - Embedded URL with `url=copy` and `text=copy` performs one clipboard
    write containing the full text.

Acceptance criteria:

```text
URL actions are mutually exclusive.
Text actions are mutually exclusive.
Exact URL payloads do not run Text action.
Embedded URL payloads run both URL and Text action.
Post-action failure does not block receive history.
```

### P.6 - File receive integration

Status: completed 2026-04-26.

Result:

```text
Added Poller._apply_receive_file_action().
Classic and streaming file receive paths classify final_path after history completion.
Image/video/document actions run before file-received callbacks.
Other file types skip action execution.
Action failures are logged and do not block callbacks or mark transfers failed.
Extended tests/protocol/test_desktop_receive_actions_poller.py for classic and streaming file actions.
python3 -m unittest tests.protocol.test_desktop_receive_actions_poller tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_config tests.protocol.test_desktop_receive_actions_settings tests.protocol.test_platform_contract tests.protocol.test_desktop_streaming_recipient: passed, 76 tests.
python3 -m unittest tests.protocol.test_desktop_streaming_integration could not run in this environment because php is not installed.
```

Implementation:

- After final file history is updated to complete in
  `_receive_file_transfer`, classify `final_path` and apply the action.
- Do the same in `_receive_streaming_transfer`.
- Run the action before `_on_file_received` callbacks so notification
  behavior remains a separate callback concern.
- Skip action execution when classification is `other`.
- Do not touch `.fn.*` command transfer handling.

Tests:

- Extend or add Poller unit tests with patched/fake
  `apply_receive_action()` to confirm both classic and streaming paths
  invoke it after a successful finalize.
- Add at least one negative test proving failed downloads/finalize paths
  do not run actions.
- Existing streaming recipient tests should still pass.

Acceptance criteria:

```text
image=open opens received images after save.
video=open opens received videos after save.
document=open opens received documents after save.
none and other only save files as before.
Classic and streaming receive paths behave the same.
```

### P.7 - Documentation and verification

Status: completed 2026-04-26.

Result:

```text
Updated desktop/README.md with Receive Actions behavior, defaults, embedded URL handling, and single clipboard-write semantics.
python3 -m unittest tests.protocol.test_desktop_receive_actions_config tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_poller tests.protocol.test_desktop_receive_actions_settings tests.protocol.test_platform_contract tests.protocol.test_desktop_streaming_recipient: passed, 76 tests.
python3 -m py_compile desktop/src/config.py desktop/src/receive_actions.py desktop/src/poller.py desktop/src/windows.py desktop/src/interfaces/shell.py desktop/src/backends/linux/shell_backend.py tests/protocol/test_desktop_receive_actions_config.py tests/protocol/test_desktop_receive_actions.py tests/protocol/test_desktop_receive_actions_poller.py tests/protocol/test_desktop_receive_actions_settings.py tests/protocol/test_platform_contract.py: passed.
git diff --check: passed.
Manual Settings smoke was not possible in this sandbox because GTK could not initialize under Xvfb.
Streaming integration tests could not run in this environment because php is not installed.
```

Implementation:

- Add a short `desktop/README.md` settings note after behavior is
  implemented.
- Leave future action types out of user docs until implemented.
- Do not add custom command support.

Verification:

```bash
python -m unittest tests.protocol.test_desktop_receive_actions_config
python -m unittest tests.protocol.test_desktop_receive_actions
python -m unittest tests.protocol.test_desktop_receive_actions_poller
python -m unittest tests.protocol.test_platform_contract
python -m unittest tests.protocol.test_desktop_streaming_recipient
```

Manual smoke:

```text
Open Settings and check section order.
Change each dropdown and inspect config.json.
Send https://example.com with url=open/copy/none.
Send plain text with an embedded URL and confirm URL and Text actions both run.
Send jpg/png/mp4/pdf/docx/zip and confirm only configured file kinds open.
```

Done when:

```text
No server or Android code changed.
All focused tests pass.
Manual receive smoke matches configured actions.
Existing default behavior remains acceptable: URL opens by default, files save only.
```

## Commit sequence

```text
P.1 config: add receive_actions defaults and migration
P.2 receive-actions: add classifier and safe action executor
P.3 platform: add shell open_path support
P.4 settings: add Receive Actions group
P.5 receiver: apply URL and text receive actions
P.6 receiver: apply file receive actions
P.7 docs: document receive action settings
```

Each `P.X` part should leave focused tests passing for the layer it
touches.

## Test plan

### Config tests

Test cases:

```text
empty config
old config with auto_open_links=true
old config with auto_open_links=false
config with missing receive_actions keys
config with invalid action values
config with unknown item types
```

### UI manual tests

```text
Open Settings.
Verify Receive Actions appears after Connection.
Verify Logs appears last.
Change each dropdown.
Close/reopen Settings.
Verify selections persisted.
```

### Receive behavior manual tests

Send from phone to desktop:

```text
https://example.com
plain text that is not URL
jpg image
png image
mp4 video
pdf document
docx document
zip archive
unknown binary file
```

For each configured action:

```text
open
copy where applicable
none
```

### Regression tests

```text
Normal file receiving still saves files.
History still updates.
Notifications still work.
Receiving unknown files still works.
Long-polling/polling behavior is unchanged.
Server protocol is unchanged.
Android app does not require update for v1.
```

## Future extensions, not part of v1

Do not implement these in the first version, but leave the config model open enough for them later:

```text
Ask every time
Reveal in folder
Open with specific app
Per-extension rules
Per-sender/device rules
Different save folders per type
Custom actions with explicit confirmation
Temporary “open but do not save” mode
```

Potential later config shape:

```json
{
  "receive_actions": {
    "url": {
      "action": "open"
    },
    "image": {
      "action": "open",
      "save_directory": "~/Pictures/Phone"
    }
  }
}
```

Do not start with this shape unless you want immediate extensibility. For v1, a simple string mapping is easier and enough.

## Recommended first commit split

```text
1. config: add receive_actions defaults and migration
2. receive-actions: add classifier and safe action executor
3. platform: add shell open_path support
4. settings: add Receive Actions group and move Logs last
5. receiver: apply URL and text receive actions
6. receiver: classify files and apply open action
7. docs: add receive actions plan / update README
```

## Final recommendation

Implement this as a small, local desktop behavior layer:

```text
received item → classify → read config → run safe built-in action
```

Avoid protocol changes, Android changes, and custom commands in v1.

The key UX change is moving from one special `Auto-open links` toggle to a clear **Receive Actions** section. Keep Settings single-page for now, but ensure **Logs is always the last section**.
