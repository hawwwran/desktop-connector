# Live testing — follow-up backlog

Items observed while driving the dev twin against the local PHP relay.
Each entry is one self-contained UX/correctness fix worth a focused
commit. Order is rough priority; not a milestone plan.

Items 1–6 below were addressed on 2026-05-07 (`tresor-vault` branch).
Status notes appear at the bottom of each section.

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

## Backlog — un-driven flows

Candidate live-test sessions that aren't yet in the chained
`docs/testing/vault-tests.md` suite. Each is one focused session
against the dev twin; findings land as items 10+ above.

Migrated 2026-05-15 from the archived
`temp/finished-plans/post-breakup-followups.md` §3.

- **Eviction under quota pressure.** Fill the relay quota, observe
  the desktop's eviction pass during upload, verify the right
  versions get culled (and that "Show deleted" surfaces them).
- **Resume upload after kill.** Start a multi-GB upload, kill the
  desktop subprocess mid-chunk, restart, verify the resume banner
  fires and the upload completes without re-uploading already-stored
  chunks. Cancel button on the resume banner (the 2026-05-06 fix —
  commit `2810201`) should also be exercised.
- **Cross-device grant + accept on a fresh device.** Exercise the
  QR-grant join flow end-to-end on the dev twin's secondary device.
- **Concurrent edits with binding sync.** Edit the same file on
  both devices between syncs; verify the conflict-rename path
  (`vault/binding/twoway.py`) produces predictable output and the
  Activity tab logs both branches.
- **Large folder bind.** Attach a folder with 10k+ small files;
  verify baseline scan completes, sync up doesn't OOM, manifest
  publishes successfully. Watch for `vault/binding/scan.py` /
  `vault/binding/watcher.py` performance cliffs.
- **Migration switch-back.** Migrate from one relay to another,
  then switch back, verify both sides agree on manifest revision.
- **Ransomware detector trip.** Simulate a mass-rewrite event in a
  bound folder; verify `vault/binding/ransomware_detector.py`
  pauses sync and surfaces the warning.
- **Schedule purge.** Set a purge schedule, fast-forward time
  (mock `_now_rfc3339` if needed), verify the scheduled purge
  fires and audits correctly.
- **Debug bundle on a real install.** Generate a bundle, inspect
  the contents, confirm no plaintext / no keys / no tokens leak
  per the logging policy in CLAUDE.md. Complements item 9's
  code-side leak-scan widening with a live-install spot check.

Already-closed candidates (kept for history): **Wrong-passphrase
rate-limit** — closed as item 7 above on 2026-05-12 (the protection
is Argon2id-intrinsic; ADR captured at
`docs/architecture-decisions.md#2026-05-12`).

When a flow lands a finding, write it up as a numbered item above
with the Symptom / Cause / Fix shape / Acceptance / Status template
and strike the bullet from this list.
