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
| Image | Open in default image viewer / No action |
| Video | Open in default video viewer / No action |
| Document | Open in default document viewer / No action |

Default values:

```json
{
  "receive_actions": {
    "url": "open",
    "image": "none",
    "video": "none",
    "document": "none"
  }
}
```

Reasoning:

- `url: open` preserves the current auto-open URL behavior.
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
    "image": "none",
    "video": "none",
    "document": "none"
  }
}
```

Allowed internal values:

```text
url:      open | copy | none
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
- Treat as URL only if the entire text is a single URL.
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
2. Detect whether it is a single URL.
3. If URL:
   - apply URL receive action.
4. If not URL:
   - keep existing text/clipboard behavior.
```

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

Likely files:

```text
desktop/src/config.py
desktop/src/windows.py
desktop/src/main.py or receiver/polling handler where incoming items are processed
desktop/src/clipboard.py if URL copy should reuse clipboard helper
desktop/src/history.py only if action result should be visible later
```

Expected changes:

### `desktop/src/config.py`

- Add default receive actions.
- Add getter/setter/helper methods.
- Add migration from `auto_open_links`.
- Keep `auto_open_links` property temporarily.

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

- Add item classification after receive/save.
- Apply configured action.
- Keep action execution non-blocking.

## Suggested implementation phases

### Phase 1 — Config only

- Add `receive_actions`.
- Migrate from `auto_open_links`.
- Add unit tests for config defaults and migration.

Acceptance criteria:

```text
New config file gets default receive_actions.
Old config with auto_open_links=true maps URL to open.
Old config with auto_open_links=false maps URL to none.
Invalid values are ignored and replaced with defaults.
```

### Phase 2 — Settings UI

- Add Receive Actions group.
- Use ComboRows.
- Remove Auto-open links from Connection.
- Move Logs to last section.

Acceptance criteria:

```text
Settings shows Receive Actions after Connection.
Changing a ComboRow persists to config.json.
Logs section appears last.
Existing URL behavior default remains open.
```

### Phase 3 — URL action

- Route received URL through `receive_actions.url`.
- Support `open`, `copy`, `none`.

Acceptance criteria:

```text
url=open opens URL as current behavior does.
url=copy copies URL and does not open browser.
url=none does not open browser and does not copy.
Non-URL text is unaffected.
```

### Phase 4 — File type actions

- Add classification for image/video/document.
- Apply `open` action via `xdg-open`.
- Keep default `none`.

Acceptance criteria:

```text
image=open opens received image in default image viewer.
video=open opens received video in default video viewer.
document=open opens received PDF/document in default viewer.
none only saves the file as before.
Unsupported files are saved as before.
```

### Phase 5 — Polish

- Add lightweight logging for action failures.
- Optional notification on action failure.
- Add small docs section in README or docs/plans.
- Add screenshots later if UI stabilizes.

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
2. settings: add Receive Actions group and move Logs last
3. receiver: apply URL receive action
4. receiver: classify files and apply open action
5. docs: add receive actions plan / update README
```

## Final recommendation

Implement this as a small, local desktop behavior layer:

```text
received item → classify → read config → run safe built-in action
```

Avoid protocol changes, Android changes, and custom commands in v1.

The key UX change is moving from one special `Auto-open links` toggle to a clear **Receive Actions** section. Keep Settings single-page for now, but ensure **Logs is always the last section**.
