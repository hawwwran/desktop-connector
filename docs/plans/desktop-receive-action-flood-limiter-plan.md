# Desktop Receive Action Flood Limiter - implementation plan

## Goal

Protect the desktop app from post-receive action flooding when many items arrive at once.

The limiter should protect local side effects only: browser tabs, app launches, and clipboard writes. It must not block download, decrypt, save, ACK, history updates, or normal transfer completion.

## Product behavior

Add configurable flood limits for each receive action type. The current Receive Actions choices still decide what action is wanted; the flood limiter only decides whether an otherwise-enabled action should run right now.

Default behavior should be protective without surprising normal usage:

| Action type | Max per batch | Max per minute |
|---|---:|---:|
| Open URL | 1 | 5 |
| Copy URL to clipboard | 1 | 10 |
| Copy text to clipboard | 1 | 10 |
| Open image | 1 | 5 |
| Open video | 1 | 2 |
| Open document | 1 | 5 |

Rules:

- `0` means unlimited for that limit.
- A batch is one pending-transfer list returned by the desktop poller.
- The rolling minute limit catches fast repeated arrivals that do not arrive in the same poll batch.
- If a limit is exceeded, the received item is still saved and marked complete; only the configured side effect is skipped.
- If actions are skipped, show one concise summary notification, for example: `Received 18 items. Skipped 15 automatic actions to prevent flooding.`
- Do not include file paths, URLs, or clipboard text in flood logs or summary text.
- Clipboard image `.fn` transfers are compatibility transport only: desktop and Android save them as `clipboard-image.<ext>` using MIME type or image signature, then treat them as normal received image files. They must not write directly into the recipient clipboard.

## Configuration model

Add a new canonical config key:

```json
{
  "receive_action_limits": {
    "url.open": {"batch": 1, "minute": 5},
    "url.copy": {"batch": 1, "minute": 10},
    "text.copy": {"batch": 1, "minute": 10},
    "image.open": {"batch": 1, "minute": 5},
    "video.open": {"batch": 1, "minute": 2},
    "document.open": {"batch": 1, "minute": 5}
  }
}
```

Validation:

- Unknown action keys are ignored.
- Missing action keys are filled from defaults.
- Negative or non-integer values are replaced with defaults.
- Very large values are clamped to a reasonable upper bound, for example `999`.
- `receive_actions` remains the source of truth for whether an action is enabled at all.

Suggested constants:

```python
RECEIVE_ACTION_KEY_URL_OPEN = "url.open"
RECEIVE_ACTION_KEY_URL_COPY = "url.copy"
RECEIVE_ACTION_KEY_TEXT_COPY = "text.copy"
RECEIVE_ACTION_KEY_IMAGE_OPEN = "image.open"
RECEIVE_ACTION_KEY_VIDEO_OPEN = "video.open"
RECEIVE_ACTION_KEY_DOCUMENT_OPEN = "document.open"
```

## Settings UI

Add a settings group directly below **Receive Actions**:

```text
Receive Action Flood Protection

Action type             Max per batch   Max per minute
Open URL                    [1]             [5]
Copy URL to clipboard       [1]             [10]
Copy text to clipboard      [1]             [10]
Open image                  [1]             [5]
Open video                  [1]             [2]
Open document               [1]             [5]
```

Implementation notes:

- Use table-style rows with headers: `Action type`, `Max per batch`, and `Max per minute`.
- Prefer numeric controls (`Gtk.SpinButton` or `Adw.SpinRow`, depending on available libadwaita support).
- Include helper text: `0 means unlimited`.
- Add a small `Reset to defaults` button for this group.
- Keep Logs as the final settings section.

## Runtime design

Add a small desktop-only limiter, either in `desktop/src/receive_actions.py` or a sibling module:

```python
class ReceiveActionLimiter:
    def start_batch(self, batch_size: int) -> ReceiveActionBatch:
        ...

    def allow(self, action_key: str, batch: ReceiveActionBatch | None) -> bool:
        ...

    def finish_batch(self, batch: ReceiveActionBatch) -> ReceiveActionFloodSummary:
        ...
```

The limiter should keep:

- per-batch action counts,
- rolling 60-second timestamp deques per action key,
- suppressed counts per action key for summary notifications,
- an injectable clock for deterministic tests.

Integration points:

- `Poller.__init__`: create one limiter instance.
- `Poller._poll_once`: create a batch context before processing pending transfers and finish it after the loop.
- `Poller._download_transfer`: pass the batch context down to receive paths.
- `Poller._apply_receive_file_action`: ask the limiter before `open_path`.
- `apply_receive_text_actions`: ask the limiter before `open_url` or clipboard-producing actions.
- Summary notification should be emitted after a batch finishes, not per suppressed item.

Important ordering:

```text
download/decrypt -> save/finalize -> ACK as currently designed -> history complete -> limiter check -> receive action if allowed -> callbacks/notifications
```

This preserves the existing "action failure does not fail transfer" contract.

## Implementation queue

### P.0 - Plan and defaults

Status: completed 2026-04-27

- Agree on action keys and default thresholds.
- Confirm `0 = unlimited`.
- Confirm that the limiter suppresses only side effects, never transfer completion.

Decision record:

- Use the six action keys from the product table: `url.open`, `url.copy`, `text.copy`, `image.open`, `video.open`, and `document.open`.
- Use the default thresholds from the product table: every action allows `1` per batch; video opens allow `2` per minute; other opens allow `5` per minute; clipboard-producing actions allow `10` per minute.
- Keep `0` as the explicit unlimited value for either the batch or minute limit.
- Scope the limiter to post-receive side effects only. Downloads, decrypts, saves, ACKs, history updates, and transfer completion must continue even when an action is suppressed.
- Treat P.1 as the first code phase: config constants, normalization, accessors, and tests.

### P.1 - Config normalization

Status: completed 2026-04-27

- Add `DEFAULT_RECEIVE_ACTION_LIMITS`.
- Add normalization, getters, setters, and reload migration for `receive_action_limits`.
- Add config tests for defaults, partial values, invalid values, and reload normalization.

Implementation notes:

- Added canonical action-key constants and `DEFAULT_RECEIVE_ACTION_LIMITS` in `desktop/src/config.py`.
- Added normalization for unknown keys, missing values, invalid values, `0 = unlimited`, and clamping above `999`.
- Added config accessors for reading, setting one limit, and resetting all flood limits.
- Added reload-time migration so edits from another process are normalized like `receive_actions`.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_receive_actions_config`
- `python3 -m py_compile desktop/src/config.py tests/protocol/test_desktop_receive_actions_config.py`

### P.2 - Limiter core

Status: completed 2026-04-27

- Add `ReceiveActionLimiter` with batch and rolling-minute enforcement.
- Add summary accounting without storing user content.
- Add focused unit tests with an injectable clock.

Implementation notes:

- Added `ReceiveActionBatch`, `ReceiveActionFloodSummary`, and `ReceiveActionLimiter` in `desktop/src/receive_actions.py`.
- The limiter enforces per-batch and rolling 60-second limits per action key.
- `0` remains unlimited for either limit.
- Suppressed actions are counted by action key only; no file paths, URLs, clipboard text, or other user content are stored in summaries.
- The limiter accepts an injectable clock for deterministic tests and reads limits through the config accessor added in P.1.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_receive_actions`
- `python3 -m unittest tests.protocol.test_desktop_receive_actions_config`
- `python3 -m py_compile desktop/src/receive_actions.py desktop/src/config.py tests/protocol/test_desktop_receive_actions.py tests/protocol/test_desktop_receive_actions_config.py`

### P.3 - Receive-flow integration

Status: completed 2026-04-27

- Thread batch context through the poller receive flow.
- Gate `open_url`, `open_path`, and clipboard-producing receive actions.
- Emit one flood summary notification after batch completion.
- Keep file callbacks and existing file-received notifications working.

Implementation notes:

- Added one `ReceiveActionLimiter` to `Poller` and create one batch context per pending-transfer poll result.
- Threaded the batch context through `_download_transfer`, `.fn` text handling, classic file receives, streaming file receives, and `_apply_receive_file_action`.
- Added limiter gates for Open URL, Copy URL to clipboard, Copy text to clipboard, and file open actions in `desktop/src/receive_actions.py`.
- Converted `.fn.clipboard.image` receives into saved `clipboard-image.<ext>` image files before applying image receive actions, so they participate in batch/minute limiting like normal image transfers.
- Treat limiter suppression as a successful no-op, so transfers stay complete and callbacks still run.
- Added one summary notification after a poll batch when actions were suppressed; the summary includes only counts, not file paths, URLs, or clipboard contents.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_poller tests.protocol.test_desktop_receive_actions_config tests.protocol.test_desktop_streaming_recipient`
- `python3 -m py_compile desktop/src/receive_actions.py desktop/src/poller.py desktop/src/config.py tests/protocol/test_desktop_receive_actions.py tests/protocol/test_desktop_receive_actions_poller.py tests/protocol/test_desktop_receive_actions_config.py tests/protocol/test_desktop_streaming_recipient.py`

### P.4 - Settings UI

Status: completed 2026-04-27

- Add the flood-protection settings group below Receive Actions.
- Add numeric rows for batch/minute limits per action key.
- Add reset-to-defaults behavior.
- Keep Logs last.

Implementation notes:

- Added **Receive Action Flood Protection** directly below **Receive Actions** in the Settings window.
- Added six configurable rows: Open URL, Copy URL to clipboard, Copy text to clipboard, Open image, Open video, and Open document.
- Each row has numeric `Max per batch` and `Max per minute` controls backed by `config.set_receive_action_limit(...)`.
- Added a `Reset to defaults` control backed by `config.reset_receive_action_limits()` and synced the visible controls after reset.
- Kept Logs as the final settings section.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_poller tests.protocol.test_desktop_receive_actions_config tests.protocol.test_desktop_receive_actions_settings tests.protocol.test_desktop_streaming_recipient`
- `python3 -m py_compile desktop/src/windows.py desktop/src/config.py desktop/src/receive_actions.py desktop/src/poller.py tests/protocol/test_desktop_receive_actions.py tests/protocol/test_desktop_receive_actions_poller.py tests/protocol/test_desktop_receive_actions_config.py tests/protocol/test_desktop_receive_actions_settings.py tests/protocol/test_desktop_streaming_recipient.py`

### P.5 - Verification

Status: completed 2026-04-27

- Run focused Python unittests for config, limiter, and receive actions.
- Compile-check desktop sources.
- Do a source inspection pass for no content-bearing logs.
- Note GTK smoke-test limitations if the sandbox cannot exercise the settings window.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_poller tests.protocol.test_desktop_receive_actions_config tests.protocol.test_desktop_receive_actions_settings tests.protocol.test_desktop_streaming_recipient`
- `python3 -m compileall -q desktop/src tests/protocol/test_desktop_receive_actions.py tests/protocol/test_desktop_receive_actions_poller.py tests/protocol/test_desktop_receive_actions_config.py tests/protocol/test_desktop_receive_actions_settings.py tests/protocol/test_desktop_streaming_recipient.py`
- `rg -n "receive_action\.(suppressed|flood_limited|flood_notification|file\.failed|text\.failed|clipboard_failed|unsupported|failed)|Receive actions limited|Skipped .*automatic|path=%s|url=%s|text=%s|clipboard text|file path|content" desktop/src/receive_actions.py desktop/src/poller.py`

Notes:

- Source inspection confirmed the flood limiter logs and summary notification use counts and action keys only, not file paths, URLs, or clipboard contents.
- Removed the receive-action file failure path from the log message while verifying this phase.
- Interactive GTK smoke testing was not run in the sandbox; the Settings window is covered here by source-level structure tests and Python compile checks.

## Review follow-up

Status: completed 2026-04-27

- Desktop `.fn.clipboard.image` receives should be saved as `clipboard-image.<ext>`, recorded as normal incoming image files, passed through image receive actions, and included in flood-limit batch/minute accounting.
- Android `.fn.clipboard.image` receives should use the same `clipboard-image.<ext>` save behavior and normal incoming file history row instead of writing the image into the recipient clipboard.
- Extension selection should prefer the encrypted metadata MIME type and fall back to common image signatures; unknown clipboard images use `.png`.

Verification:

- `python3 -m unittest tests.protocol.test_desktop_receive_actions tests.protocol.test_desktop_receive_actions_poller tests.protocol.test_desktop_receive_actions_config tests.protocol.test_desktop_receive_actions_settings tests.protocol.test_desktop_streaming_recipient tests.protocol.test_desktop_message_contract tests.protocol.test_android_clipboard_image_source`
- `./gradlew :app:compileDebugKotlin`

## Non-goals

- No server protocol changes.
- No Android flood-limit settings in this phase.
- No transfer rejection or sender throttling.
- No prompt/confirmation dialog before every action.
- No per-sender policy in v1; this can be added later if needed.
