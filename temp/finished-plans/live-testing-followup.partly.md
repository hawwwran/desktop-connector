# Live testing — completed items (§1–§14)

Carved out of `docs/plans/live-testing-followup.md` on 2026-05-19. The un-driven flow backlog (B6 / B5 / B4 / B2 / B1) stays in `docs/plans/`; everything below is the historical record of what was found-and-fixed while driving the dev twin against the local PHP relay.

Items observed while driving the dev twin. Each entry is one self-contained UX/correctness fix worth a focused commit. Order is rough priority; not a milestone plan.

Items 1–6 below were addressed on 2026-05-07 (`tresor-vault` branch). Status notes appear at the bottom of each section.

---

## 1. Async Argon2id during vault create — show progress

**Symptom**: clicking **Continue** on the create-passphrase step of the
onboarding wizard freezes the window for many seconds. The user reported
"the onboarding window froze after I clicked Continue. Only after very
long time it continues."

**Cause**: `Vault.prepare_new()` runs Argon2id key derivation
synchronously on the GTK main thread (`desktop/src/windows_vault.py`
~line 2111, inside `perform_create()` which is called from `on_pp_next`).
Argon2id is intentionally memory-hard / slow; on this machine the
default parameters block the UI loop long enough that the window stops
repainting and looks crashed.

**Fix shape**:

- Move `Vault.prepare_new` (and any follow-on `save_local_vault_grant`
  + `publish_initial` work the wizard does inline) into a worker
  thread. Use the same `threading.Thread` + `GLib.idle_add` pattern
  the Folders tab already uses for `add_remote_folder` etc. — there
  are several worked examples in `desktop/src/vault_folders_tab.py`.
- While the worker runs, the wizard must visibly explain *why* it's
  waiting. Don't just disable the button; users read a frozen window
  as broken.
  - Switch the visible child to a transient "Deriving key…" panel
    with a `Gtk.Spinner` + a one-line explanation:
    *"Stretching your passphrase with Argon2id. This is intentional —
    it's what stops attackers from brute-forcing your vault."*
  - Optional: show the Argon2id memory + iteration parameters so the
    UX matches the security-narrative ethos ("64 MiB, 3 iterations").
- On success → swap to the existing success screen as today.
- On failure → swap back to the passphrase step with `pp_status` set
  to the error (today's failure path).

**Why visible progress matters**: per memory `feedback_no_fake_tests.md`
and `feedback_security_ux.md`, the project's stance is that
security-critical waits should be honest about what's happening — a
spinner without context is theatre. The Argon2id wait is real work
that *protects* the user; the UI should say so.

**Acceptance**:

- Window stays responsive during create; can be moved / minimised /
  resized while derivation runs.
- A new "Deriving key…" panel appears immediately on Continue and
  disappears when the success screen lands.
- No regression in the existing failure paths (phase-2 grant save
  failure, phase-3 relay publish failure with Retry publish).
- Worker exception → main thread idle_add → user-visible error on the
  passphrase step (no swallowed traceback).

**Test surface**:

- Source pin in `tests/protocol/test_desktop_vault_a11y_source.py` (or
  similar) for the new `Gtk.Spinner` + the worker-thread shape.
- Existing wizard logic stays decoupled from GTK: the
  `Vault.prepare_new` call doesn't change. Easy to verify the worker
  body behaves identically.

**Status (2026-05-07): done**. `windows_vault.py:perform_create` now
runs phases 1–4 in a worker thread; the wizard shows a "Deriving key…"
panel with a spinner while Argon2id stretches. F-LT01 marker.

---

## 2. Single-threaded PHP test relay starves UI requests

**Symptom**: clicking "Add" in Vault Settings against the local
`php -S 127.0.0.1:4441` relay took ~50 seconds. PHP access log showed
the manifest GET landing exactly 25 seconds after the click and the
PUT another 25 seconds after that — both exactly the
`/api/transfers/notify` long-poll timeout.

**Cause**: `php -S` is single-threaded. The headless dev twin's poller
sits in a 25-second long-poll on `/api/transfers/notify`; every other
request (including the vault settings window's manifest GET/PUT)
queues behind it. CLAUDE.md already calls this out for the dashboard.

**Fix shape**:

- For local testing, document in `docs/testing/vault-tests.md` that
  the headless dev twin must NOT run while exercising vault UI flows
  on the same `php -S` relay. The receiver only matters for transfer
  tests; vault tests can do without it.
- Optional belt-and-braces: the vault automation harness could spawn
  a second PHP worker on a separate port for the receiver, leaving
  the first port free for UI traffic. Not worth it unless we need
  both halves running simultaneously.

**Acceptance**: harness guide explicitly carves vault tests as
"vault-only" (no headless receiver) so a future test session doesn't
re-discover this on a 50-second add-folder click.

**Status (2026-05-07): done (doc-only)**. Added "Vault UI tests run
**without** the headless dev twin" callout to
`docs/testing/vault-tests.md` between the GTK4 window command block
and the per-test artefact section.

---

## 3. Onboarding leaves orphan vault rows on retry

**Symptom**: dev twin server DB ended up with two `vaults` rows after
a single completed onboarding session — `7HUYW3AIGUHD` (abandoned
first attempt) and `AQMJFDLP7Y62` (the keeper, what `config.json`
points at). The user only saw one onboarding flow finish; the orphan
came from an aborted first attempt.

**Cause**: `Vault.prepare_new` + `publish_initial` writes the new
vault row to the relay before `config.save` records the
`last_known_id` on disk. If the user closes the wizard or re-runs
onboarding before the success path commits, the relay keeps the row
and config never references it. Next attempt creates a fresh
`vault_id` → orphan.

**Fix shape**:

- Strict-mode: don't `publish_initial` until the user has acknowledged
  the recovery-kit panel; or alternatively, on subsequent prepare in
  the same session, reuse the in-memory `_pending_publish` payload
  (already idempotent) instead of generating new vault material. This
  is the cleaner option because it preserves the "first publish is
  retryable" property.
- Lenient mode: a `DELETE /api/vaults/{vault_id}` endpoint scoped to
  "vault-author-on-this-device-only" so the wizard can clean up an
  abandoned vault on retry. Big surface change; not worth it unless
  we add a server-side GC story too.
- UI: show a "Resume previous attempt" affordance if `state["vault"]`
  has `has_pending_publish=True` when the wizard re-opens.

**Acceptance**: completing a single onboarding session leaves exactly
one row in the relay's `vaults` table.

**Status (2026-05-07): partial**. F-LT03 in
`windows_vault.py:perform_create` now folds phases 3 (publish) + 4
(config commit) into a single worker call so the orphan window
between "row on relay" and "id in config.json" is microseconds rather
than the full duration of the success-screen-render path. Worker
also honours a `wizard_cancelled` `threading.Event` set by `on_close`
so a window dismissed during Argon2id derivation never advances to
keyring or relay writes. Cross-session orphans (a row from a prior
abandoned wizard run that already published) still need either a
scoped relay DELETE or a "Resume previous attempt" UI affordance —
both deferred per the original fix-shape options; not in this pass.

**Status (2026-05-12): done** on `tresor-vault`. Path A landed via
`desktop/src/vault/resume.py` + `windows_vault/onboard_window.py`:
in-config `pending_publish` marker written after `save_local_vault_grant`
and cleared atomically alongside `last_known_id` on success; wizard
launch detects the marker and offers Resume (re-derive recovery
material, PUT-header or POST under the same vault id) or Discard
(confirmation gate per `feedback_security_ux`, then delete grant
+ clear marker). Acceptance verified by 12 tests in
`tests/protocol/test_desktop_vault_resume.py`; decision logged at
`docs/architecture-decisions.md#2026-05-12`.

---

## 4. Vault Browser doesn't auto-refresh after a sync

**Symptom**: after dropping a file into a bound folder and clicking
"Sync now" successfully (toast: "1 uploaded"), the Vault Browser
window still shows the pre-sync manifest. Closing and reopening the
browser shows the new file. Same will happen for any background-sync
event — the open Browser stays frozen on whatever revision it loaded
at open time.

**Cause**: the Vault Browser fetches the manifest once at open and
doesn't subscribe to manifest revision changes. The Sync now publish
that bumped the revision happens in a different process (the Vault
Settings subprocess); even if Browser were to listen for events it
would need cross-process notification.

**Fix shape options** (rough preference order):

- **Pull on focus**: re-fetch the manifest when the Browser window
  regains focus (`Gtk.Window.notify::is-active` or
  `Gdk.Surface::enter-monitor`). Cheap, reactive, no IPC.
- **Pull on a timer**: 5–10 second poll while the window is visible;
  cheaper than a watcher but burns idle requests.
- **Cross-process signal**: Settings publishes a "manifest-changed"
  marker file or D-Bus signal; Browser listens. Most "correct" but
  the most plumbing.

**Acceptance**: drop a file, click Sync now in Settings, glance back
at an already-open Browser → file appears within a few seconds with
no manual refresh.

**Status (2026-05-07): done**. F-LT04: `windows_vault_browser.py`
connects `notify::is-active` on the browser window and re-runs
`refresh_manifest_async()` whenever the window regains focus, gated
on `refresh_btn` being sensitive so we don't queue a second refresh
behind an in-flight one.

---

## 5. Vault Browser detail panel + file list layout

**Symptom**: the right-side **Details** panel in the Vault Browser
(`desktop/src/windows_vault_browser.py`, around line 299) renders as
a 2-column label/value grid where labels are right-aligned in a
narrow column and long values (filename, full path, version id,
fingerprint) are crammed into the rest of the strip. Long file paths
and the `fv_v1_…` version id wrap or get truncated mid-word. The
file's name itself is rendered as one of the rows (`Name | <value>`)
rather than as a heading for the whole panel.

The file list grid on the left has the same flavour of bug we already
fixed in the Folders tab: a `Gtk.Grid` whose columns let cells like
the Modified ISO timestamp wrap char-by-char into a tall narrow
column, which in turn makes the row height balloon.

**Wanted shape** (per user feedback while testing 2026-05-07):

- Selected file's **name** at the top of the panel as a bold heading
  on its own row — not a label/value pair.
- Each detail underneath is a stacked pair: **label** (bold, dim
  caption font, on its own line) → **value** on the next line,
  selectable / monospace where appropriate (paths, version ids,
  fingerprints), with a small bottom margin before the next pair.
- Fields in order: Path, Logical size, Remote stored size, Modified,
  Current version, Versions, Status.
- Use `Gtk.Box` (vertical) of pair-boxes instead of a `Gtk.Grid`.
- Long values (path, version id) get `set_ellipsize(MIDDLE)` plus
  a tooltip with the full string, like the bindings card path.

**Also fix the file list grid** in the same pass: the Modified column
needs the same ellipsize/tooltip treatment OR a fixed display format
that doesn't wrap (e.g. `2026-05-07 11:00`).

**Acceptance**: a long path / long version id no longer wraps into a
tall narrow column; the file name is visually distinct as the panel
heading instead of a label/value row.

**Status (2026-05-07): done**. `windows_vault_browser.py:render_detail`
now leads with a bold ellipsize-middle heading carrying the file's
name and tooltip; underneath, each detail is a stacked
label-then-value `Gtk.Box` pair, with `EllipsizeMode.MIDDLE` +
tooltip on long values (path, version id) and `selectable=True`
throughout for copy-out. The file-list grid's wrap-into-tall-column
issue resolves naturally with the 19-char fixed timestamps from item
6.

---

## 6. Display timestamps in local timezone, fixed-width format

**Symptom**: every timestamp the user sees in the Vault Browser
(Modified column in the file list, Modified field in the details
panel, Modified column in the Versions table) is rendered as the raw
RFC-3339 string from the manifest — e.g. `2026-05-07T11:00:47.000Z`.
That's the wire format; users have to do timezone math in their head
and the trailing `.000Z` is noise.

**Wanted**: render every user-facing timestamp in the device's local
timezone, in `YYYY-MM-DD HH:MM:SS` form (24-hour, no fractional
seconds, no timezone suffix).

**Fix shape**:

- Single helper module — e.g. `desktop/src/vault_time_format.py` —
  with `format_local(rfc3339: str) -> str` and a fallback that
  passes the original string through unchanged when parsing fails
  (defensive: a malformed manifest entry shouldn't blank a row).
- Use `datetime.fromisoformat` (Python 3.11+ accepts the trailing
  `Z`); fall back to `dateutil` only if we hit pre-3.11 support
  needs, which we don't on this codebase.
- Apply at every timestamp render site:
  - `windows_vault_browser.py` — file list `modified`, details
    panel `Modified`, Versions table `Modified`.
  - Any other vault window showing manifest/version timestamps
    (audit Activity tab, version history dialogs, etc.).
- The wire format stays RFC-3339 UTC — display-only transform.

**Why fixed-width**: the file-list grid currently wraps the long
ISO string char-by-char (item 5); switching to
`YYYY-MM-DD HH:MM:SS` (19 chars, no wrap) plus a non-wrapping label
fixes the layout without needing a tooltip.

**Acceptance**: file list, details panel, and Versions table all show
e.g. `2026-05-07 13:00:47` for a UTC `2026-05-07T11:00:47.000Z`
manifest entry on a `+02:00` host. No `T`, no `Z`, no fractional
seconds visible to the user anywhere.

**Status (2026-05-07): done**. New helper
`desktop/src/vault_time_format.py:format_local()` parses RFC-3339
(including trailing `Z`), converts to the device's local timezone,
and renders `YYYY-MM-DD HH:MM:SS`. Defensive: empty/None → `""`,
unparsable → original string. Wired at every browser timestamp
render site: file list `modified`, details `Modified`, deleted/
recoverable-until tombstone label, status-strip "Deleted —
recoverable until …" suffix, Versions table `modified`, version
download status, restore-confirm heading. Activity-tab timestamps
use a different epoch path and were not in the listed scope.

---

## Add new items below as live testing surfaces them.

---

## 7. Wrong-passphrase rate-limit is Argon2id-implicit, not keyring-backed

**Symptom**: `temp/finished-plans/post-breakup-followups.md` §3 listed
"wrong-passphrase rate-limit — verify the keyring-backed retry budget
and the human-readable error path" as a candidate live-test flow.
Reading the code, no such budget exists.

**Cause**: protection comes entirely from Argon2id's intrinsic cost
(`vault/recovery_kit.py:verify_recovery_kit` calls
`derive_recovery_wrap_key` at v1-locked params m=128 MiB / t=4 ≈ 1-10 s
per attempt). `windows_vault/tab_recovery.py` simply re-enables the
Test button after each attempt — no counter, no cooldown, no lockout.
The same holds in `vault/export/bundle.py` for the import flow.

**Fix shape**: this is a doc drift, not a missing feature. At v1 params
a generated 7-word passphrase has ≈ 84 bits of entropy; Argon2id at
m=128 MiB takes ≈ 1-10 s/attempt; an offline brute-force is infeasible
even without a lockout. Online attempts are bounded by physical access
+ Argon2id wall-clock. Recommend rewording the §3 line to "verify
Argon2id-implicit rate-limit + human-readable error path", and adding a
note to `docs/architecture-decisions.md` that v1 deliberately ships
without an explicit retry counter (rejected as redundant given the
Argon2id cost floor).

**Acceptance**: post-breakup-followups.md §3 wording updated; either an
ADR entry or a paragraph in CLAUDE.md captures the Argon2id-as-
rate-limit decision.

**Status (2026-05-12): done**, documentation-only. ADR entry at
`docs/architecture-decisions.md#2026-05-12` (Wrong-passphrase
rate-limit is Argon2id-implicit). Plan §3 line in
`post-breakup-followups.md` reworded to drop the
"keyring-backed retry budget" framing (since archived to
`temp/finished-plans/post-breakup-followups.md`).

---

## 8. Conflict-naming attempt-exhaust silently overwrites a pre-existing file

**Symptom**: under §A20 conflict resolution, the
`_unique_conflict_path` helper caps at
`_MAX_CONFLICT_PATH_ATTEMPTS = 20`. On exhaust it logs
`vault.sync.conflict_naming_attempts_exhausted` and returns the last
candidate — which already exists by definition (that's why the loop
continued). The caller then writes to that path, silently overwriting
a pre-existing conflict copy.

**Cause**: two call sites:

- `desktop/src/vault/ops/restore.py:548` — restore-time conflicts.
  After 20 collisions, `_download_one(target=conflict_target)` writes
  to a path that already exists.
- `desktop/src/vault/binding/twoway.py:733` — two-way sync conflicts.
  Same shape; the inline comment ("silently growing the path is the
  worse failure") acknowledges the trade-off but the actual exhaust
  behavior is silent overwrite, not growth.

The 20-attempt cap is reachable only when the user runs ≥ 20 conflict-
producing operations in the same UTC minute against the same path with
the same device name — a pathological churn pattern. But the data-loss
window is real.

**Fix shape**: on exhaust, fall back to a longer disambiguator (UUID,
microsecond timestamp, or a count file in the parent dir) instead of
returning a path the caller will then overwrite. Two-line patch each
site.

**Acceptance**: test asserts that after exhausting 20 candidates,
`_unique_conflict_path` returns a path that does NOT exist on disk.

**Status (2026-05-12): done** on `tresor-vault`. F-LT07:
`make_conflict_path` gained a `random_token=` parameter; both
`_unique_conflict_path` helpers (restore + twoway) now fall back to a
4-byte hex-token loop after the 20-attempt numeric cap. Coverage in
`tests/protocol/test_desktop_vault_conflict_naming.py`
`ConflictPathExhaustFallbackTests` asserts the returned path doesn't
collide.

---

## 9. Debug bundle leak scan has narrow gaps for URL-safe base64 and
   token-like field names

**Symptom**: the debug-bundle redaction + leak scan
(`vault/diagnostics/debug_bundle.py`) has two narrow gaps the existing
test suite doesn't cover. Both are future-leak risks rather than
current leaks (the present config.json shape is exhaustively
catalogued in `REDACT_KEYS`), but the leak scan is the "last line of
defence" — its blind spots matter.

**Cause**: 

1. **URL-safe base64.** `FORBIDDEN_PATTERNS` regex 6 requires
   `[A-Za-z0-9+/]{43,44}` (standard base64). `secrets.token_urlsafe(32)`
   outputs `[A-Za-z0-9_-]{43}` (url-safe). The vault uses url-safe in
   `vault_access_secret = secrets.token_urlsafe(32)` (`vault/vault.py`
   `prepare_new`). If a future code path landed that secret into
   config.json under a field name that escapes `SENSITIVE_KEY_SUBSTRINGS`
   (e.g. "bearer", "session"), the value scan would miss it (too short
   for the 100+ FCM-token regex, wrong character class for the 43-44
   base64 regex).
2. **`SENSITIVE_KEY_SUBSTRINGS` is conservative.** The tuple is
   `("secret", "recovery", "passphrase", "master_key", "authorization",
   "purge")`. Notable absences: "token", "bearer", "credential",
   "private". A future "fcm_token" / "session_token" / "private_key"
   field would not be caught by name; only by value (and only if its
   value matches FORBIDDEN_PATTERNS — see point 1).

**Fix shape**:
- Add `"token"`, `"bearer"`, `"credential"`, `"private"` to
  `SENSITIVE_KEY_SUBSTRINGS`. (Five-character "secret" already implies
  "secret" but is independent of "token".)
- Extend the base64-32-bytes regex to a second alternative for url-
  safe: `[A-Za-z0-9_-]{43,44}` (with the same right-side word-boundary
  guard).
- Add a test that round-trips a `token_urlsafe(32)` value through
  `scan_for_forbidden` and asserts at least one pattern matches.

**Acceptance**: a `secrets.token_urlsafe(32)` placed in a non-redacted
field of a test config is caught by `build_debug_bundle_bytes` and the
bundle raises `DebugBundleError` rather than writing the secret.

**Status (2026-05-12): done** on `tresor-vault`. F-LT07:
`SENSITIVE_KEY_SUBSTRINGS` extended with `token` / `bearer` /
`credential` / `private`; `FORBIDDEN_PATTERNS` gained a second 32-byte
shape matching url-safe `[A-Za-z0-9_-]{43,44}` alongside the existing
`+/` form. Coverage in
`tests/protocol/test_desktop_vault_debug_bundle.py`
`ScanForbiddenTests.test_urlsafe_base64_32_byte_secret_is_caught` plus
the new `SensitiveKeySubstringsTests` class.

**Side note (not a bug)**: `binding_states.json` intentionally
includes `local_path` per the bundle's docstring. For a user attaching
the bundle to a public support thread, the home-directory path is
mildly sensitive. Worth flagging in the bundle export UI ("your
binding paths will be included") or trimming to leaf-only on export.
Design decision, not a bug — but recording here so a future "private
mode" toggle has a known scope.

---

## 10. No client-side manifest-revision floor — rollback undetectable

**Symptom**: surfaced by the 2026-05-15 critical-risks evaluation
gate (`docs/vault-critical-risks-evaluation.md` §3.7). The
architecture doc acknowledges that a fresh-device restore cannot
detect a relay-driven manifest rollback before it has seen any
state; the risks doc's requirement is broader — *every* client
should remember the highest manifest revision it has ever seen and
warn / refuse when the relay serves a lower revision.

**Cause**: grep across `desktop/src/vault/state/` and the manifest
GET path finds no `highest_seen_revision`, `revision_floor`, or
`last_known_revision` persistence. The quick integrity check
(`desktop/src/vault/ops/integrity.py:77–133`) validates
`parent_revision == head_revision - 1` *within* a served manifest
chain but not across sessions. A relay that previously served
revision K can later serve revision K - N and the client accepts
it silently — the local index simply rebuilds from whatever the
relay claims.

**Fix shape**:

1. Add a `highest_seen_revision` int column / key alongside the
   existing local-state persistence (probably
   `vault-local-index.sqlite3` next to the per-binding rows; the
   exact home is a design call). Bumped on every successful
   manifest decrypt.
2. On manifest GET, compare the served `revision` against the
   stored floor. If `served < floor`:
   - log `vault.manifest.rollback_detected
     served=<N> floor=<K> vault_id=<id>` (event added to
     `docs/diagnostics.events.md`);
   - surface a persistent warning banner in Vault Settings
     ("This relay served an older state than we've seen before
     — possible relay tampering or data loss. Run integrity
     check.");
   - **do not** auto-apply the served manifest until the user
     either confirms or restores from export.
3. Document the brand-new-device limitation explicitly in the
   warning copy: a fresh restore cannot have a floor yet, so the
   user sees this warning *only* if there is divergence after a
   prior unlock on this device.

**Acceptance**:
- Unit test in `tests/protocol/test_desktop_vault_manifest.py`
  (or a new file) constructs a fake relay that serves revision K
  then K-1; asserts the second call produces a `RollbackDetected`
  typed result rather than silent acceptance.
- Live-test pass updates the warning copy + screenshot.
- `docs/vault-critical-risks-evaluation.md` §3.7 status flips
  from **Open** to **Resolved** and the summary table updates.

**Status (2026-05-15): done** on `tresor-vault`. F-LT10 ships in two
commits: (1) `feat(vault): manifest rollback detection — per-device
revision floor` (typed exception + SQLite floor + diagnostic event +
11 base tests); (2) the latched-flag persistence + `Adw.Banner` in
Vault Settings (`desktop/src/windows_vault/rollback_banner.py`) +
self-heal on next successful decrypt. Coverage in
`tests/protocol/test_desktop_vault_rollback.py` (20 tests).
`docs/vault-critical-risks-evaluation.md` §3.7 flipped Open →
Resolved.

---

## 11. Fresh-unlock requirement bypassed in import + destructive UI

**Symptom**: surfaced by the 2026-05-15 critical-risks evaluation
gate (`docs/vault-critical-risks-evaluation.md` §3.9, §3.11). The
architecture doc §12 and the risks doc both call for fresh unlock
on sensitive operations (clear vault, hard purge, rotate access
secret, revoke device, rotate recovery, import-merge into existing
vault) **regardless of the unlock timeout setting**. In practice,
the wizards open the vault via the cached device grant without
re-prompting for the recovery passphrase.

**Cause**: `open_local_vault_from_grant()` (and the equivalent
unlock helpers used by `windows_vault_import.py` and
`tab_danger.py`) load the grant from the system keyring directly.
There is no "is this within the fresh-unlock window?" check before
landing on the danger-zone confirm screens or the import-merge
commit screen. The timeout setting at §13 of the architecture doc
governs *idle reauth*, not *sensitive-op reauth* — those are
described as two different gates but the codebase only enforces
the first.

**Fix shape**:

1. Add a per-process `fresh_unlock_at: float` timestamp set on
   successful passphrase entry (recovery test, onboarding,
   explicit "Unlock now"). Stored in memory only — never
   persisted.
2. Define `FRESH_UNLOCK_WINDOW_S` (suggestion: 120 s; short
   enough to mean "the user *just* typed the passphrase", long
   enough to walk through the typed-confirm UI).
3. Gate entry to:
   - `tab_danger.py` clear-folder / clear-vault / schedule-purge
     button handlers;
   - `tab_security.py` rotate-access-secret / rotate-recovery /
     revoke-device handlers (once that tab lands);
   - `windows_vault_import.py` commit-merge handler.
   On gate failure, surface an inline "Unlock with recovery
   passphrase to continue" mini-prompt that re-runs Argon2id and
   verifies via the existing recovery test path.
4. The mini-prompt **does not** reset the cached grant — it only
   stamps `fresh_unlock_at`. Cached grant continues working for
   non-sensitive ops until the regular timeout fires.

**Acceptance**:
- Unit / integration test asserts that calling the destructive
  helpers without a fresh-unlock stamp raises
  `vault_unlock_required`.
- Manual live-test: clear-folder dialog refuses to commit
  without the fresh-unlock mini-prompt; entering the passphrase
  once stamps the window and subsequent typed-confirm screens
  reuse the stamp until it expires.
- `docs/vault-critical-risks-evaluation.md` §3.9 and §3.11 flip
  from **Mitigated** to **Resolved**; summary table updates.

**Status (2026-05-15): done** on `tresor-vault`. F-LT11 ships:
- `desktop/src/vault/fresh_unlock.py` — per-process in-memory
  stamp with `FRESH_UNLOCK_WINDOW_S = 120 s`, injectable clock
  for tests, typed `FreshUnlockRequiredError`.
- `desktop/src/windows_vault/fresh_unlock_prompt.py` — inline
  `Adw.Dialog` mini-prompt with kit picker + passphrase entry;
  re-runs Argon2id via `verify_recovery_kit`; stays open across
  failed retries; stamps on success.
- Gate sites: `tab_danger.py` (clear-folder, clear-vault,
  schedule-purge) and `windows_vault_import.py` (merge-commit)
  funnel through `require_fresh_unlock_or_prompt` before any
  destructive worker kicks off. Source-pinned in
  `test_desktop_vault_danger_zone_source.py` +
  `test_desktop_vault_import_wizard_source.py`.
- Stamp also set on successful recovery test in
  `tab_recovery.py` (same proof, no need to re-prompt within the
  120-s window).
- Diagnostic events:
  `vault.fresh_unlock.{verified,verify_failed,prompt.envelope_meta_missing}`
  added to `docs/diagnostics.events.md`.
- 12 new unit tests in
  `tests/protocol/test_desktop_vault_fresh_unlock.py` cover the
  stamp lifecycle, the 120-s expiry boundary, restamp refresh,
  clear, and the typed-error gate.
- `docs/vault-critical-risks-evaluation.md` §3.9 and §3.11 both
  flipped Mitigated → Resolved.

With F-LT11 done the evaluation gate reads **0 Open / 1
Mitigated** (§3.3 — UX wording + per-role server gates, both
deferred to the post-v1 Devices tab). Vault v1 can be stamped.

---

## 12. Resume-after-kill banner — pass with three minor UX gaps (was B8)

**Symptom**: B8 in the backlog ("Start a multi-GB upload, kill the
desktop subprocess mid-chunk, restart, verify the resume banner fires
and the upload completes without re-uploading already-stored chunks").
Driven 2026-05-16 on suite 0003; full result writeup at
`temp/automation-tests-results/0003/B8-resume/result.md`.

**Cause / Verification**:
- `vault/upload/single_file.py:259` rewrites the
  `<session_id>.json` atomically after every chunk PUT, so a SIGKILL
  or `SyncCancelledError` exit leaves the same on-disk shape. The
  banner's `list_resumable_sessions(vault_id, cache_dir)` filter
  picks any session where `phase != "complete"`.
- Drove a 150 MB random file (75 × 2 MiB chunks) via a direct
  `upload_file()` call with `should_continue` returning False after
  25 chunks. Session JSON `7ce097c51aafd2ea.json` written
  (`phase="uploading"`, chunks 25/75 done).
- Vault Browser launched: banner renders the exact copy from
  `resume_banner.py:51-57` — *"1 upload was interrupted — click
  Resume to finish it, or Cancel to discard."*
- **Resume**: 50 new chunk PUTs (no re-upload — `batch_head_chunks`
  HEAD-and-skip honored), manifest CAS-published as revision 3,
  session JSON cleared. Server `vault_chunks` count: 25 → 75.
- **Cancel** (second iteration with a different file so the
  same-fingerprint `skipped_identical` path doesn't short-circuit):
  session JSON cleared, banner hidden, server-side chunks **not**
  deleted (95 → 95 — matches the `resume_banner.py:67-69`
  docstring; eviction/retention claims them later).

**Three minor UX gaps** (none block B8 PASS):

1. **SO-1 — Stale dev-twin keyring entries survive suite-start wipes**
   (harness bug, not vault bug). `rm -rf ~/.config/desktop-connector-dev`
   does not clear the `desktop-connector-dev` keyring service.
   Cross-suite cruft (`auth_token`, `private_key:pem`, `device_id`,
   `vault_grant:*` for vault ids the relay no longer knows about)
   made the dev twin boot reporting *"Already registered as …"*
   while the server `devices` table was empty, then 401 on every
   call. Suite 0003 worked around it explicitly; add a "clear
   dev-twin keyring service" step to
   `docs/testing/vault-tests.md`'s suite-start block, or have
   `Config.__init__` clear the secret store when the config dir
   doesn't exist on disk.
2. **SO-2 — Cancel status text doesn't mention orphan chunks.** After
   Cancel the relay still holds the chunks that were PUT before
   the abort — by design, they're claimed by storage maintenance.
   The current "Discarded N interrupted upload sessions" doesn't
   say so; consider widening to "Discarded N interrupted uploads.
   Any chunks already uploaded will be cleaned up by storage
   maintenance." (one-line change in `resume_banner.py:91-94`).
3. **SO-3 — Browser doesn't render the newly-uploaded file inline
   after Resume.** Resume worker sets `self.state.manifest = last_manifest`
   and calls `_render_all()` on the GLib.idle_add path, but the
   root-folder view doesn't pick up the new manifest's folder
   contents — only the detail pane refreshes. Re-launching the
   browser shows the file. Same shape as item 4 (focus-based
   refresh); likely a follow-up there.

**Acceptance**: see assertions in
`temp/automation-tests-results/0003/B8-resume/result.md`. All eight
assertions pass; the three UX gaps are listed as side observations
SO-1, SO-2, SO-3.

**Status (2026-05-16): done** on `tresor-vault`. B8 backlog item
struck. Three follow-up nudges (SO-1/2/3) recorded in the result
file; if they become work, they get their own numbered items.

---

## 13. Large folder bind — works, but 10k = 2 hours wall-clock (was B7)

**Symptom**: B7 in the backlog ("Attach a folder with 10k+ small
files; verify baseline scan completes, sync up doesn't OOM, manifest
publishes successfully. Watch for `vault/binding/scan.py` /
`vault/binding/watcher.py` performance cliffs"). Driven 2026-05-16
on suite 0004; full result writeup at
`temp/automation-tests-results/0004/B7-large-folder/result.md`.

**Cause / Verification**: step-ladder bind of 100 / 1k / 10k random
256-byte files, each in a nested subdirectory tree, against a clean
vault on a single-threaded php -S relay.

| Folder | scan wall | sync wall | rate (start → end) | RSS peak |
|---|---|---|---|---|
| 100 files | 0.16 s | ~3 s | ~30 ops/s | 57 MiB |
| 1 000 files | 1.46 s | 70.4 s | 26.7 → 14.2 ops/s | 67.3 MiB |
| 10 000 files | 14.5 s | **7908 s (2 h 11 min)** | 8.5 → 1.3 ops/s | 210 MiB |

- **Scan is fine** — linear in N with no per-file IPC.
- **Sync drain is the cliff.** Rate decays roughly as `k / sqrt(N)`
  (8.5 ops/s → 1.3 ops/s over 10 000 ops). Manifest grows linearly,
  each `upload_file` call ships the full encrypted manifest down + up.
- **Memory is fine** — peak 210 MiB at 10k entries, scaling ~13 KiB
  per cached entry. A 100k-file binding would extrapolate to ~2 GiB
  RSS which is concerning, but 10k is the spec.
- **Correctness holds** — all 10 000 ops `status=uploaded`, manifest
  CAS-published from revision 1104 → 11 104 with no conflicts during
  the serial single-client run. No OOM, no failed ops, no broken
  versions.

**Three improvement targets** (none block B7 functional PASS):

1. **Redundant manifest GET per op.** ~~`run_backup_only_cycle`
   (sync.py:328-336) does `vault.fetch_manifest(relay)` after every
   successful op for the next op's view — even though
   `publish_manifest` already returned the updated manifest dict and
   the loop has it in `current_manifest`. Per-op cost is 2 manifest
   round-trips where 1 would do. **Expected ~2× speedup** by trusting
   the publish_manifest return value (re-fetch only when a CAS
   conflict signals divergence).~~ **DONE 2026-05-16** as SO-2 on
   `tresor-vault` (commit `8ffba34`).
2. **No batched-publish path.** ~~Every file is its own manifest
   revision. Total bytes shipped scales as `O(N²)`. A future
   "accumulate K ops, publish one combined manifest revision" API
   would collapse 10k revisions into 100 (K=100), dramatically
   shortening initial-bind time.~~ **DONE 2026-05-16** as SO-3 on
   `tresor-vault` (commit `a93ba08` + review fixes `08401d5`).
3. **Initial-bind UX has no rich progress.** A user dropping a
   Documents folder into a binding will wait ~2 h with only an
   "X/Y synced" counter — no ETA, no warning. Worth either a
   one-time toast ("Large folder; this may take a while — N files
   queued") at bind time, or a richer progress widget that
   surfaces the rate trajectory.
   Phase 1 of the perf plan (commit `c61bc42`) added the slow-bind
   warning dialog + Phase 1.5 (`0da1736`) added the ambient
   "Vault sync K/N" banner. ETA suffix in the banner is still open
   (low-priority; the now-much-faster 21 min bind reduces the need).

**Post-SO-2/SO-3 numbers** (clean dev twin, `php -S`,
2026-05-16):

| | Baseline | Post-fix | Speedup |
|---|---|---|---|
| 1k bind | 70.4 s | 17.0 s | 4.1× |
| 10k bind | 7908 s (2 h 11 min) | 1230 s (20 m 31 s) | 6.4× |
| 10k publishes | 10 000 | 200 (K=50) | 50× fewer |

All ops uploaded, zero failures, manifest CAS-published cleanly.
The 6.4× combined result (vs the plan's predicted ~50×) reveals
that manifest publishes were one cliff but chunk-PUT serial cost
on single-threaded `php -S` is now the next one — Apache mod_php
should land closer to the predicted ceiling. Estimator calibration
follow-up in the perf plan's "Open questions".

**SO-1 (B7) — Stray dev twins survive `kill $stored_pid`**: while
setting the test up, four orphan headless `src.main` processes
accumulated. Each `kill $TWIN` from a setup retry was killing the
**bash shell wrapper PID** (`echo $!` after backgrounding a
`python3 -m src.main …` captures the wrapper, not the python
child), so the python twin survived each kill. Discovered when a
microsync showed **86 s per file** instead of 22 ms — PHP server
log was full of `/api/transfers/notify` 25-second long-polls from
four parallel pollers, starving every other request. `pkill -9 -f
"src.main.*desktop-connector-dev"` cleaned them up; rate
immediately returned to ~22 ms/file. **Reinforces B8 SO-1** —
both are between-runs hygiene gaps in
`docs/testing/vault-tests.md`'s suite-start.

**SO-4 (B7) — Watcher path not exercised**: ~~this test drove the
`scan_for_local_changes` + Sync-now path, not the inotify-driven
`vault_filesystem_watcher`. Both feed into the same pending-ops
queue, but the watcher enqueues incrementally instead of in burst.
A future B7-followup should drop 10k files into a binding **with
the dev twin running** to verify the watcher doesn't lose events
under burst load.~~ **DONE 2026-05-16** on `tresor-vault`:

- `tests/protocol/test_desktop_vault_filesystem_watcher.py:WatcherBurstLoadTests`
  drives 10 000 events into `WatcherCoordinator` directly with a
  hermetic clock and verifies every path lands in pending-ops with
  no drops / no duplicates / no synthesised paths. Three vectors:
  10k creates with stable stat, 10k deletes filtered through the
  §A17 / T12.2 predicate, 10k mixed create+modify events
  collapsing to one op per path.
- `WatchdogObserverBurstSmokeTests.test_200_create_burst_reaches_coordinator_pending`
  exercises the real `watchdog.Observer` thread + inotify path
  with 200 files (skipped when `python3-watchdog` isn't installed;
  bounded to ≤ 5 s wall-clock for CI). The unit-test scale at 10k
  already exercises the coordinator's hot path; this layer just
  pins the watchdog→coordinator adapter.

A live-session 10k drop against the dev twin (full daemon, real
inotify, full sync drain to relay) would be belt-and-braces but
isn't required — the unit-scale 10k + real-inotify 200 covers
both halves of the integration. File it as a follow-up if a real
user reports lost events.

**Acceptance**: see assertions in
`temp/automation-tests-results/0004/B7-large-folder/result.md`.
All five assertions pass; three improvements (SO-2/3/4 above)
plus SO-1 (harness hygiene) recorded as follow-up nudges.

**Status (2026-05-16): done** on `tresor-vault`. B7 backlog item
struck. The two ~2× perf wins (skip redundant manifest GET; batched
publish) are real and would dramatically improve initial-bind UX
for large user folders. If they become work, they get their own
numbered items.

---

## 14. Migration round-trip against real PHP relays — works (was B3)

**Symptom**: the §5.C1 migration wizard landed 2026-05-18 with the
engine pinned by `test_desktop_vault_migration_runner.py`'s
``FakeMigrationRelay`` unit tests, but no test had ever driven
``run_migration`` against a real PHP relay. The risk this catches
is HTTP-layer + PHP-side regressions: response envelope drift,
auth-rate-limit interactions, the genesis-author-mismatch path
relaxed in `b048d86`, and the migration-propagation
``previous_relay_url`` side-effect on every paired client.

**Cause**: gap in coverage, not a bug. The wire-protocol contract
tests in `test_server_contract.py` exercise individual endpoints in
isolation; the migration runner exercises the state-machine in
isolation. The integration of "engine drives real HTTP client
against real PHP" is the missing surface.

**Fix shape**: new
`tests/protocol/test_desktop_vault_migration_live.py` spins two
hermetic `_ServerHarness` instances (the existing helper from
`test_server_contract`), each with a config override flipping
``migrationAllowPrivateUrls`` to `true` so the relay accepts the
peer's `127.0.0.1:<random-port>` URL. The test creates a vault on
relay A via `Vault.create_new` (Argon2id at reduced 64-MiB / 2-iter
params to keep wall-clock under 500ms) and runs `run_migration` A→B
through real HTTP, asserting `verify.matches=True`, root-revision
parity on both relays post-migration, and that `get_header` still
succeeds on the source after commit.

**Scope decision**: genesis-vault migration only (no file uploads,
no folder publishes). With files the test would exceed
`VaultAuthService::AUTH_LIMIT` of **10 calls per (device, vault) per
minute** before migration starts — `Vault.create_new` + per-folder
publishes + 2 uploads = ~15 auth-billed calls. The chunk-copy path
is the same code in both genesis and populated cases, so the live
test catches the HTTP-integration regressions that matter; the
``FakeMigrationRelay`` suite covers the broader chunk-content
scenarios.

**Acceptance**:
- ``verify.matches`` returns `True` after the engine commits A→B.
- ``relay_a.fetch_root_manifest`` and ``relay_b.fetch_root_manifest``
  agree on `root_revision` post-migration.
- ``relay_a.get_header`` still resolves on the source after the
  commit step (the vault row is marked migrated, not deleted).
- Server log records ``vault.sync.migration_propagation_applied``
  exposing the new ``previous_relay_url`` + expiry — the §5.M6
  switch-back grace window is wired through the HTTP boundary.

**Status (2026-05-19): done** on `tresor-vault`. Wall-clock for
the test is ~440ms; the full vault suite holds 1113/1113 green
with the new test added.

Findings worth a follow-up:
- The B→A switch-back leg is NOT exercised (each leg burns the
  per-vault auth budget; running both consecutively trips
  `vault_rate_limited`). Either bump the limit via a future
  ``vaultAuthLimit`` config knob or add a ~60s sleep between legs.
  Not blocking — switch-back propagation is independently covered
  by the unit-level tests.
- File-upload coverage in the live test would need either
  (a) configurable rate limits, (b) `time.sleep(60)` between phases,
  or (c) load-balancing API calls across multiple registered
  devices for the same vault. Filed as a follow-up; not on v1.

---

## 15. Eviction under quota pressure — server emission verified, GUI flow deferred (was B5)

**Symptom**: B5 in the backlog ("Fill the relay quota, observe the
desktop's eviction pass during upload, verify the right versions get
culled"). Driven 2026-05-19 on suite 0005; full result writeup at
`temp/automation-tests-results/0005/B5-eviction/result.md`.

**Cause / Verification**: clean dev twin against `php -S` relay,
vault `DMVT2PG6PLC3` with 1 folder + 5 × 1 MiB random files in a
backup-only binding. Quota forced to 4 194 304 bytes via direct
`UPDATE vaults SET quota_ciphertext_bytes = …` (the
`vaultQuotaBytes` config key the recipe relied on turned out to be
dead code — see SO-1 below).

| Phase | Setup | What the server emitted | Client triage path |
|---|---|---|---|
| Phase 1 (auto-purge boundary, no candidates) | quota=4 MiB, 5 fresh files | **HTTP 507** `used=3145848 quota=4194304` after 3 chunks landed | `describe_quota_exceeded → alarm=False, eviction_available=False` → "Vault is full and no backup history remains" terminal banner |
| Phase 2 (alarm condition) | quota lowered to 2 MiB after step 1 | **HTTP 507** `used=3145848 quota=2097152` | `→ alarm=True` → "Vault quota was reduced — approve cleanup" passphrase dialog |
| Phase 3 (synthetic eviction-available) | probe-only, no relay call | `eviction_available=True` | `→ alarm=False, eviction_available=True` → "Vault is full — making space" silent auto-purge |

Server emission + client-side triage (`describe_quota_exceeded`)
cover all three UX routes correctly. Each 507 logged
``vault.sync.upload_quota_exceeded binding=… path=… used=N quota=N``
on the client; the alarm-condition emit carried `used > quota`
verbatim, so no quota-shrink-specific code is needed on the server.

**Three side observations** (none block B5 PASS):

1. **SO-1 — `vaultQuotaBytes` config key is dead code.**
   `server/data/config.json`'s `vaultQuotaBytes` is referenced by
   `CLAUDE.md` and `skipped-while-autonomous.md` but **never read by
   server code**. `VaultsRepository::create` issues an `INSERT INTO
   vaults` that omits the quota column; rows fall through to the
   schema default at `migrations/002_vault.sql:31` —
   `DEFAULT 1073741824` (1 GB). The recipe + CLAUDE.md need
   updating, or the config key needs wiring into the create path.
2. **SO-2 — AUTH_LIMIT=10/(device,vault)/min blocks live eviction
   workflows.** A single 5-file sync trips the limit before the
   batch-end shard publish lands (batch-head + root fetch + chunk
   PUTs + shard publish = >10 calls). Each retry replays into the
   same wall. Same gap §14's migration test flagged; a
   `vaultAuthLimit` config knob would unblock both.
3. **SO-3 — Chunks land before shard publishes; failed publish
   orphans them.** Phase 1's 3 successful chunk PUTs sat orphaned
   on the relay because the shard-with-root publish 429'd. Expected
   behaviour of the SO-3-batched publish design — chunks are
   idempotent and HEAD-deflated on retry, §4.M1 orphan reaper
   sweeps eventually — but means `eviction_pass` can't be
   exercised against real state until at least one sync cycle
   completes through to a published shard.

**Acceptance**: see the result writeup. The two un-driven items —
full GUI eviction flow + `eviction_pass` against live state —
require either AT-SPI driving (alarm dialog, passphrase prompt) or
SO-2 untied (so a sync completes through the shard publish).

**Status (2026-05-19)**: **partial PASS** on `tresor-vault`. Server
emission + client triage logic verified end-to-end. GUI dialog
drive + algorithm walk filed as follow-ups; recipe in
`docs/plans/skipped-while-autonomous.md` needs the SO-1 / SO-2
caveats added.

**Update (2026-05-19, commit `242c5ff`)**: SO-1 + SO-2 landed.
``vaultQuotaBytes`` config key now wired through
``VaultsRepository::create``; ``vaultAuthLimit`` config key added
with a floor of 10 so operators can raise the cap on dedicated
hosts without weakening the §1.H1 throttle. See ADR
[`2026-05-19 — Vault config knobs`](../../docs/architecture-decisions.md).
SO-3 (orphan chunks until shard publishes) is by design and stays.

---

## 16. Eviction algorithm walk + EvictionRelay Protocol drift fix (was B5 follow-up)

**Symptom**: with SO-2 now landed (``vaultAuthLimit`` config knob), the
B5 result writeup's "eviction_pass not exercised against real state"
gap is unblocked. Driven 2026-05-19 on suite 0005/B5-followup-eviction
against the same dev-twin harness as §15, this time with
``vaultAuthLimit=200`` so a full sync cycle (chunk PUTs + batch-head +
root fetch + shard publish) finishes inside one budget window.

**Cause / Verification**: setup phase confirmed SO-1 wired correctly —
``Vault.create_new`` against a config-set ``vaultQuotaBytes=8388608``
landed exactly that value on the new ``vaults`` row's
``quota_ciphertext_bytes`` column (first live verification, since the
B5 phase 1 test forced quota via direct SQL). Sync of 5 × 1 MiB
files completed cleanly: 5 chunks active, 5,243,080 used, root
revision 3, **zero 429s** — confirming SO-2's premise that the floor-
protected limit unblocks legitimate sync workloads. Overwriting 3
files to build multi-version state then partially succeeded (file-1 +
file-3 uploaded new versions; file-2 hit 507 at the quota boundary
with ``used=7,340,312 quota=8,388,608``).

State going into the eviction walk: 7 active chunks (5 originals + 2
new versions), 2 old-version chunks now eligible as eviction
candidates per the algorithm's "oldest non-current version" rule.

**Bug surfaced**: calling ``eviction_pass`` from a probe script
raised ``TypeError: VaultHttpRelay.gc_plan() got an unexpected
keyword argument 'manifest_revision'``. Root cause: the
``EvictionRelay`` ``Protocol`` in ``desktop/src/vault/ops/eviction.py``
declared ``gc_plan(*, manifest_revision: int, ...)`` while the
production ``VaultHttpRelay.gc_plan`` in ``binding/runtime.py``
actually accepts ``root_revision=``. The two test fakes that backed
the eviction unit suite matched the **Protocol**, not the
production class — so every existing eviction test passed
green while the real HTTP path raised ``TypeError`` on every
invocation. ``eviction_pass`` had never run against the real relay.

**Fix shape (landed in this same session)**: rename
``manifest_revision`` → ``root_revision`` across the Protocol +
both ``gc_plan`` call sites in ``eviction.py`` (eviction stage +
``reap_orphan_chunks``) + the test fake's parameter + the fake's
recorded plan dict. All 1113 vault tests stay green; the change is
purely a kwarg-name realignment with no behavior shift.

**Algorithm walk after fix**: same probe, this time with
``target_bytes_to_free=1,500,000`` and ``mode="auto"``:

| Metric | Before walk | After walk |
|---|---|---|
| ``used_ciphertext_bytes`` | 7,340,312 | **5,243,080** (−2,097,232) |
| ``current_root_revision`` | 4 | **6** (one bump per stage publish) |
| Active chunks | 7 | **5** |
| ``eviction_pass`` result | n/a | ``bytes_freed=2,097,232 chunks_freed=2 target_met=True`` |
| Stage events | n/a | 2 × ``vault.eviction.auto_purged_oldest`` |

Bytes freed (2,097,232) is exactly 2 × 1,048,616 — both old-version
chunks purged, matching the algorithm's "oldest non-current versions
first, one at a time until target met or candidates exhausted"
contract. Each stage published a fresh shard revision (root went 4 →
5 → 6).

**Re-drain after eviction**: re-running ``run_backup_only_cycle``
picked up the stuck file-2.bin op, found space (used + chunk now
6,291,696 ≤ 8,388,608), and uploaded cleanly. Pending ops queue
drained to zero. The "507 → eviction → retry succeeds" loop holds
end-to-end against real state.

**Acceptance**: see assertions inline above. All four phases
(SO-1 verification, multi-version build, eviction walk, post-purge
re-drain) succeeded; the Protocol drift bug fix is regression-pinned
implicitly by the test fakes now matching the production signature.

**Status (2026-05-19)**: **PASS** on ``tresor-vault``. Closes the B5
follow-up. The §15 GUI eviction dialog drive (alarm + passphrase
prompt) is still un-driven, but that's an AT-SPI concern, not an
algorithm one; the destructive purge logic is now proven correct
against the real HTTP relay.

---

## 17. Migration switch-back leg works — round-trip A→B then B→A (was B3 follow-up)

**Symptom**: §14 (2026-05-19) landed the genesis-leg live migration
test (A→B) but explicitly deferred the switch-back leg (B→A back-to-back)
because two consecutive legs on the same ``(device, vault)`` pair brushed
up against the hardcoded ``VaultAuthService::AUTH_LIMIT=10/minute``. The
2026-05-19 ADR added the ``vaultAuthLimit`` config knob (server-side
floor 10, no ceiling), and `live-testing-followup.md` flagged this leg
as "now unblocked".

**Cause**: gap in coverage, not a bug. The switch-back path stresses the
engine's idempotent re-entry into the target-side bootstrap (the target
relay still has the vault row from a previous lifecycle — on leg 2 the
"target" is the original genesis relay), plus the §H2 7-day grace
``previous_relay_url`` propagation on the SECOND leg.

**Fix shape**: new ``VaultMigrationLiveSwitchBackTests`` class
alongside the existing genesis class in
``test_desktop_vault_migration_live.py``. The class bumps
``vaultAuthLimit`` to 30 via ``_ServerHarness`` config_overrides
(well above the ~5 auth-billed calls per leg per pair, with headroom
for verify-step retries under HTTP flake). Distinct ``setUpClass``
from the genesis class — re-using the same harnesses would leak the
bumped limit into the genesis test, weakening its "we fit under
floor=10" assertion.

The test runs A→B then immediately B→A on the same in-memory
``Vault`` object (the ``vault_id`` + ``master_key`` +
``vault_access_secret`` are stable; only relay orientation flips).
Each leg writes its own ``vault_migration.json`` to a dedicated
config dir so a post-mortem failure can distinguish leg-1 (cleared)
from leg-2 (final state).

**Acceptance**:
- ``leg1.verify.matches`` and ``leg2.verify.matches`` both ``True``.
- Root revision parity on both relays after each commit.
- ``get_header`` resolves on BOTH relays post-round-trip (the row
  stays on both sides — marked migrated, not deleted — so the
  §H2 switch-back UI affordance is reachable).
- Engine's ``vault_already_exists`` idempotent re-entry handles the
  leg-2 target-side existing row gracefully (A had the vault from
  ``Vault.create_new``; on leg 2 the engine re-encounters it as
  migration target and must not crash).
- ``vault.sync.migration_propagation_applied`` log line inverts on
  the second hop: leg 1 records ``previous=A_url``, leg 2 records
  ``previous=B_url`` (asserted via ``assertLogs`` capture around
  each post-commit ``get_header`` call, which is where the
  propagation handler at ``runtime.py:213-255`` actually runs).

**Scope statement (not an engine invariant)**:
- ``leg1.chunks_copied`` and ``leg2.chunks_copied`` are both ``0``
  in this test because the test creates a genesis vault — no folder
  publishes, no file uploads. The engine doesn't *guarantee* zero
  chunks; the scope was chosen to keep wall-clock low and avoid
  retesting the chunk-copy contract that
  ``test_desktop_vault_migration_runner.py``'s ``FakeMigrationRelay``
  suite already covers exhaustively.

**Status (2026-05-19): done** on ``tresor-vault``. Total wall-clock
~500ms for the new test (on top of the genesis test's ~440ms).
``vault.sync.migration_propagation_applied`` fires twice — once per
leg with the right ``new``/``previous`` URLs:

```
leg 1: new=B, previous=A, expires=2026-05-26T…
leg 2: new=A, previous=B, expires=2026-05-26T…
```

Full vault suite holds 1170/1170 green with the new test added (+1
over §14's 1169 count).

Closes the B3 follow-up bullet from `live-testing-followup.md`. No
new findings — both the engine's idempotent re-entry and the
propagation side-effect work symmetrically on the second hop.
