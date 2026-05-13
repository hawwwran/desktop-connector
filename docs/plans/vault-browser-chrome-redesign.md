# Vault Browser chrome redesign

**Date opened:** 2026-05-13
**Branch:** `tresor-vault`
**Status:** Waves 1, 1.5, 2 done 2026-05-13. Wave 2's optional
`Adw.NavigationSplitView` responsive-collapse wrapper is deferred —
the sidebar redesign landed without it; revisit if narrow-window
usage becomes a concern.

## Why this exists

The Vault Browser's top toolbar is a flat strip of 8 always-visible
pill buttons plus a checkbox:

```
[ Back ][ Forward ][ Refresh ][ Upload ][ Upload folder ][ Delete ][ Versions ][ Download ]  [ ☐ Show deleted ]
```

All buttons render at all times — they just dim when inactive. There
is no grouping, no separation between navigation / creation /
destructive / primary actions, and the toolbar sits **inside the
window body** above the breadcrumb rather than in the window chrome.
The "Refresh" and "Show deleted" entries are not actions but
controls / settings; "Versions" is effectively a label for the panel
below it.

The left "folder tree" pane is a vertical `Gtk.Box` of plain
`Gtk.Button`s — no icons, no selection state, no expanders, no
indentation. It diverges from the Vault Settings window's left
sidebar (which uses the Adwaita navigation-sidebar pattern via
`Gtk.StackSidebar`) and from every other GNOME app shipped today.

This plan brings the browser in line with the Adwaita 1 conventions
already used by `windows_vault/main_window.py` and
`windows_settings/window.py`, and reduces the chrome's perceived
weight from "8 things competing for attention" to "1 primary action +
1 menu when idle; 3 contextual actions when a row is selected".

## Target shape

### Header bar

`Adw.HeaderBar` (replaces the inline `Gtk.Box` strip):

- **Start edge**
  - Flat icon button `go-previous-symbolic` — folder-navigation Back.
    Bound to the existing `self.back_btn` slot for blast-radius
    minimisation (the rest of the mixin code already toggles it).
  - **Forward is dropped.** File-manager forward is web-browser
    muscle-memory that no GNOME file app ships in 2026. The breadcrumb
    + Back covers every real navigation. The `self.forward_btn` slot
    stays as `None` so the existing `_update_nav_buttons` no-ops the
    Forward branch; the `BrowserState.forward` field can stay
    unchanged.
- **Center**
  - Window title via `Adw.WindowTitle` — defaults to "Vault" with the
    current path as subtitle. Wave 1.5 moves the body breadcrumb into
    this slot; Wave 1 keeps the breadcrumb in the body for diff
    minimisation.
- **End edge**
  - `Adw.SplitButton` — primary action "Add". Click = "Upload file…",
    arrow popover = "Upload folder…". Backed by the existing
    `self.upload_btn` (primary click) and `self.upload_folder_btn`
    (folder click) slots, so all the other mixins' sensitivity-toggle
    code continues to work.
  - `Gtk.MenuButton` (`open-menu-symbolic`) — overflow:
    - Refresh (bound to `self.refresh_btn`'s click handler; the slot
      remains a `Gtk.Button` so other mixins can `set_sensitive` it).
    - Show deleted (toggle — bound to `self.show_deleted_toggle`).
    - (Wave 2+) Storage info, Help, etc.

### Selection-driven action bar

A `Gtk.Revealer` directly below the header bar, hidden when no row is
selected. When a file is selected, reveals an `Adw.Bin` styled as a
toolbar with three buttons:

- **Download** (`suggested-action`) — bound to `self.download_btn`.
- **Versions** — bound to `self.versions_btn`. Hidden for folders.
- **Delete** (`destructive-action`) — bound to `self.delete_btn`.

When a folder is selected (or just navigated into), the revealer
shows Download (for the folder) + Delete (for the folder).
`_render_all` already knows the right sensitivity per context — no
new logic, just a `revealer.set_reveal_child(...)` call.

### Sidebar (Wave 2)

Replace the current `Gtk.Box`-of-buttons with a `Gtk.ListBox` styled
with the Adwaita `navigation-sidebar` CSS class. Each row:

- folder icon (`folder-symbolic`)
- folder display name
- indentation per depth via row margins
- selection highlight (built into the CSS class)

Wrap the whole split in `Adw.NavigationSplitView` so the sidebar
collapses to a hamburger on narrow windows — the canonical Adwaita 1
shape that Files, Calendar, and the Settings sidebar all use.

## Sequencing

Three waves, one PR each, behaviour-byte-identical where possible.

### Wave 1 — header bar + selection-driven action bar

Goal: get the chrome shape right. No sidebar changes; breadcrumb stays
in the body for diff minimisation.

Files touched:
- `desktop/src/windows_vault_browser/layout.py` — rewrite
  `_build_action_bar` to populate an `Adw.HeaderBar` instead of a body
  `Gtk.Box`. Adds a `Gtk.Revealer` for the selection-driven action bar.
- `desktop/src/windows_vault_browser/app.py` — `_on_activate` stops
  adding an empty `HeaderBar`; `_build_action_bar` now owns that. Add
  a slot for the revealer and call `set_reveal_child` from
  `_render_all` / `_render_detail` paths.

Slot-name preservation: every existing `self.<name>_btn` slot stays in
place so the other mixins (`uploads.py`, `downloads.py`,
`delete_restore.py`, `quota.py`, `resume_banner.py`, `panes.py`) need
zero changes. The `forward_btn` slot stays `Optional[Gtk.Button] | None`
with a `None` value so `_update_nav_buttons` no-ops it.

### Wave 1.5 — breadcrumb into header title

Move the body `self.breadcrumb` label into the header bar's
`Adw.WindowTitle` subtitle slot. `_render_all` already drives it via
`set_label` — swap to `set_subtitle`. One-file change in `layout.py` +
`app.py`.

### Wave 2 — sidebar + NavigationSplitView

Replace `_build_panes` start-pane with a `Gtk.ListBox` styled
`navigation-sidebar`; rewrite `_render_tree` to populate `ListBoxRow`s
instead of plain buttons. Wrap the outer split in
`Adw.NavigationSplitView`. The center file list + right detail panes
stay unchanged.

## Open questions

- **Forward button**: dropped per plan. If user testing later shows
  someone misses it, the slot is still wired — re-add as a menu item
  in the hamburger.
- **Show-deleted-as-toggle-row in menu**: the menu item needs to render
  as a checkbox. `Gio.Menu` supports stateful actions; alternatively,
  pop a custom `Gtk.CheckButton` into a `Gtk.Popover`. Wave 1 picks
  the simpler stateful-action route.
- **Selection-bar transition**: a slide-down `Gtk.Revealer` matches the
  Files / Photos pattern; could also use `Adw.Banner` with no message,
  just actions. Files / Photos pattern wins on familiarity.

## Acceptance

### Wave 1

- The Vault Browser opens with an `Adw.HeaderBar` containing Back +
  SplitButton(Add) + MenuButton, not the body button strip.
- With no selection, no contextual action bar is visible.
- Selecting a file reveals Download / Versions / Delete in the bar.
- Selecting / navigating into a folder reveals Download / Delete.
- Refresh works from the menu.
- Show deleted works from the menu and round-trips through
  `self.show_deleted_toggle.get_active()`.
- Upload, Upload folder, Download, Delete, Versions all dispatch to
  the same handlers as today (no behavioural change — only the chrome).
- `./test_loop.sh` green; existing vault-browser dogtail tests
  updated to drive the new chrome.

### Wave 2

- Sidebar visually matches the Vault Settings left sidebar.
- Narrow-window mode collapses the sidebar to a hamburger.
- Folder selection state is visible; keyboard navigation works.

## Test impact

- `tests/protocol/test_desktop_vault*.py` — any that asserts widget
  layout (source-pin tests in particular) needs review. Search for
  `back_btn`, `upload_btn`, `action_bar` literals.
- Dogtail / AT-SPI tests targeting the Vault Browser by button name
  ("Upload" → now lives inside a SplitButton popover, named "Upload
  file…"; "Refresh" → menu item, not button). Surface this in the
  Wave 1 PR description so the test sweep lands alongside the UI
  change.
- No protocol or wire-format changes — purely UI.

## Notes for whoever picks this up

- Slot names are load-bearing across the mixins. Renaming
  `self.upload_btn` to e.g. `self.add_button` would force changes in
  6+ files. Keep the slots; just change what widget backs them.
- Pointer-cursor styling is applied via `apply_pointer_cursors` after
  `present()` — works for header-bar widgets the same as body widgets.
- The existing `Adw.HeaderBar` at `app.py:177` is the line to replace.
