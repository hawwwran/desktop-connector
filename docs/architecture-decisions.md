# Architecture Decisions

This file is the long-term tracker for **non-trivial architectural
decisions** in Desktop Connector. When a choice would otherwise force a
future maintainer to re-derive the reasoning from code or commit
history — protocol shape, persistence strategy, security boundary,
cross-runtime contract, irreversible data-format pick, dependency
adoption / removal — write it down here.

This complements (not replaces) the existing surfaces:

- `CLAUDE.md` → the "Key design decisions" section captures the legacy
  / cross-cutting decisions in narrative form. Going forward, *new*
  decisions land **here** as discrete dated entries; CLAUDE.md gets a
  one-liner pointer when relevant.
- `docs/plans/` → working notes for in-flight features; ephemeral.
- `docs/protocol/` → the wire spec; describes what is, not why.
- `docs/diagnostics.events.md` → emitted-event catalog.
- `temp/vault-found-issues/` (gitignored) → review backlog tracker.

## When to add an entry

Yes:

- Choosing a wire format, encryption mode, or AAD shape.
- Settling on a state machine (e.g. transfer phases, sync binding
  states, migration recovery transitions).
- Picking between two non-obvious implementations and rejecting the
  other on durable grounds.
- Adding / removing a dependency or build tool.
- Changing a security boundary or threat-model assumption.
- Adopting a backwards-incompatible behaviour gate.

No (skip the entry, just commit normally):

- Bug fixes that restore intended behaviour.
- Single-call refactors / renames / cleanups.
- UI copy or styling tweaks.
- Test reorganisation.
- Anything fully captured by a clear commit message + the code it
  changes.

## Entry format

Each entry is a level-3 heading dated `YYYY-MM-DD`. The body is short —
prose, not bullets — and answers four questions:

1. **What** did we decide.
2. **Why** that and not something else (the rejected alternatives).
3. **Where** the decision is anchored in code (paths, key functions).
4. **Status** — `accepted` / `superseded by <date>` / `deprecated`.

Newer entries go on top so the latest decisions are visible without
scrolling.

### Template — copy this for new entries

```markdown
### YYYY-MM-DD — Short title (verb-led, ≤ 70 chars)

**Status:** accepted.

**Context.** What problem we're solving and why now.

**Decision.** What we chose. One paragraph max.

**Alternatives.** What we rejected and why — the bit future-you will
want when arguing whether to flip the decision.

**Anchor.** Files / functions / commits that implement it.
```

---

## Entries

### 2026-05-11 — Vault subsystem consolidates under `desktop/src/vault/`

**Status:** accepted.

**Context.** The vault subsystem accreted as ~52 flat top-level
`vault_*.py` modules in `desktop/src/`, alongside a small `vault/`
core package and the already-split `vault_upload/` / `vault_download/`
packages. Cohesion was invisible (the 10 `vault_binding_*.py` files
form a state machine; the filesystem didn't show them as a group),
imports forked across two styles (flat + package), and new
contributors couldn't tell plumbing from data ops from UI glue.

**Decision.** All vault data-layer code lives under
`desktop/src/vault/` (89 files, 12 subpackages, 14 top-level
modules). The lone surviving top-level **`vault_folders/`** is the
**Folders TAB GTK widget tree** — UI, not vault logic. The
data-layer name for folder concerns is **singular** (`vault.folder`);
the GTK tab keeps the **plural** top-level name. Two unavoidable
triple-dot imports remain inside `vault/binding/runtime.py` reaching
the non-vault top-level `crypto.py` / `connection.py` — those don't
belong under the vault subsystem.

A regression-guard test
(`tests/protocol/test_desktop_vault_no_legacy_paths.py`) AST-parses
every `desktop/src/*.py` and `tests/protocol/*.py` for `vault_X`
import shapes; anything outside `ALLOWED_VAULT_PREFIXED`
(`folders`, `submenu`) fails CI. The scanner disambiguates module
re-introduction (`from . import vault_X` where `vault_X.py` is on
disk → fail) from harmless `__init__.py` symbol re-exports
(`from .. import vault_id_dashed` where no file exists → ignore).

**Alternatives.** (a) Keep the flat namespace and rely on
alphabetic ordering — rejected: 52 files is past the point where
the filesystem stops being a useful index. (b) Move only the
obvious clusters (binding, grant, migration) and leave the rest
flat — rejected: produces a worse layout than either extreme,
with half the subsystem in `vault/` and half flat siblings,
giving new code two equally-valid homes. (c) Single big-bang PR —
rejected by the breakup plan's principle "incremental, mechanical,
byte-for-byte moves are reviewable; a 100-file rename is not."
Waves A–G followed that discipline; 769 unit tests + 32 vault-v1
cross-runtime vectors stayed green at every commit boundary, with
several execution-time bugs caught by the suite and fixed
in-commit. (d) Hoist the redundant `subpackage/subpackage.py`
files (e.g. `vault.grant.grant`) into their `__init__.py` —
rejected for Wave G in favour of semantic renames
(`vault.grant.store`, `vault.export.bundle`, `vault.migration.state`,
`vault.import_.bundle`), which give each submodule a name that
describes what's inside. (e) Regex-based regression guard —
rejected (after a first implementation): regex missed three
common import shapes (`from . import vault_X`,
`from .. import vault_X`, `from src import vault_X`) and couldn't
distinguish module re-introductions from `__init__.py` symbol
re-exports. AST parsing handles every shape and the filesystem
check resolves the symbol ambiguity.

**Anchor.** `desktop/src/vault/` is the package. The full Wave A–G
commit range is `dad6a9e^..0f79917`. Plan docs:
`docs/plans/desktop-file-size-breakup.md` (original scope) +
`docs/plans/post-breakup-followups.md` (wave progression). The
filesystem layout (subpackages and their contents) is enumerated
there; cross-link rather than duplicate. Older entries in this
file anchor at pre-consolidation paths like
`vault_crypto.derive_recovery_wrap_key` — those live under
`vault/crypto.py` now; one hop suffices.

### 2026-05-06 — Recovery secret is one-shot — kit re-export requires header rotation

**Status:** accepted (formalises an existing constraint that wasn't
written down).

**Context.** The vault recovery flow uses two-of-two material: the
user's passphrase AND a 32-byte ``recovery_secret`` generated at
vault create time. The wrap key for the recovery envelope is
``HKDF-SHA256(salt=argon2id(passphrase, argon_salt), ikm=recovery_secret)``
(``vault_crypto.derive_recovery_wrap_key``, formats §12.3). Both
inputs are required — compromise of either alone yields no wrap
key. The recovery envelope (AEAD-encrypted ``master_key``) lives
**inside the relay-side encrypted vault header**
(``vaults.encrypted_header`` in the SQLite schema, served from
``GET /api/vaults/{id}/header``). The kit file is the **only**
copy of ``recovery_secret`` outside that envelope.

This was implicit until suite 0002 test 07 made it operational: a
user (or the harness) who clicked through the wizard without
exporting the kit lands in a state where there is no way to produce
a kit later from any combination of passphrase + cached grant +
config metadata. Argon2id over the saved ``argon_salt`` produces an
``argon_out`` that needs ``recovery_secret`` to mix in, and
``recovery_secret`` is a 32-byte random value that lives only in
the kit file.

**Decision.** Document the constraint explicitly so future work
treats it as a load-bearing security property, not an oversight:

1. **There is no "re-export the same kit"** code path. Once a kit
   exists, the only way to produce another one is to **rotate**
   the recovery material — generate a fresh ``recovery_secret`` +
   ``argon_salt``, re-wrap ``master_key``, and **re-publish the
   relay-side header** at ``header_revision = current+1`` so a
   fresh-device recovery sees the new envelope. ``master_key``
   itself does not change; only the envelope that wraps it.
2. **A rotation is a header-revision bump**, not a desktop-only
   operation. Local rotation that doesn't touch the relay would
   leave the old envelope on the relay, so any new kit produced
   locally would fail fresh-device recovery (the device would pull
   the old header and try to unwrap with the new ``recovery_secret``).
3. **The "Update recovery material" UI surface remains disabled**
   (``windows_vault.py:198``,
   ``update_recovery_btn.set_sensitive(False)``,
   tooltip "Recovery-material rotation is not implemented yet")
   until both the local re-derivation **and** the
   ``PUT /api/vaults/{id}/header`` rotation path land — a real
   T-N feature, not a harness unblock.
4. **Wizard copy already advertises this**: the success-step
   warning "Your data is unrecoverable without BOTH the recovery
   kit file AND your passphrase. There is no password reset."
   matches the crypto.

**Alternatives considered.** (a) Persist ``recovery_secret`` in
keyring alongside the grant so it could be re-fetched — rejected:
makes a single device's keyring compromise sufficient to forge a
kit (defeats two-of-two). (b) Derive ``recovery_secret``
deterministically from ``master_key`` — rejected: same reason,
plus removes any independent factor. (c) Allow a
"local-only rotation" that doesn't re-publish the header —
rejected: produces a kit that works locally but fails on a fresh
device, which is the scenario the kit is meant to cover.

**Anchor.** Crypto: ``vault_crypto.derive_recovery_wrap_key``
(line ~644), ``vault_crypto.build_recovery_envelope``
(line ~680). Generation: ``vault.Vault._prepare_local`` /
``Vault.create`` (line ~299, ``recovery_secret = secrets.token_bytes(32)``).
Format spec: ``docs/protocol/vault-v1-formats.md`` §12. The
constraint is what blocked harness suite 0002 test 07 (now removed
from the guide); see ``temp/automation-tests-results/0002/test-07/result.md``
for the full diagnosis trail.

### 2026-05-06 — Vault grant keyring service is per-config, not a hard-coded constant

**Status:** accepted.

**Context.** Suite 0002 test 06 found that test 04's vault grant for
`QRJCRIE7AXEU` (created by the dev twin running with
`--config-dir=~/.config/desktop-connector-dev`) had landed in keyring
service `desktop-connector` — the canonical user's namespace. Root
cause: `desktop/src/vault_grant.py` had `_KEYRING_SERVICE = "desktop-connector"`
hard-coded as a module-level constant; `KeyringGrantStore.save` /
`load` / `delete` / `has_grant` all called the keyring API with that
constant regardless of which `config_dir` the caller threaded in.

This is the third instance of the same bug shape on 2026-05-06: the
`auth_token` keyring (fixed earlier via `Config.config_dir.name`
auto-derivation) and the file-manager XDG scripts dir (fixed via
config-id markers) had the identical symptom — a non-default
`--config-dir` reaching into a per-user shared OS resource without a
per-install discriminator. `Config` was the obvious gateway, but it
isn't the only place that talks to the keyring; `vault_grant.py`
opens its own `KeyringGrantStore` independently.

**Decision.** `vault_grant._resolve_keyring_service(config_dir)`
derives the service name from `Path(config_dir).name`, mirroring
`Config.__init__`'s logic byte-for-byte. The default install
(`config_dir.name == "desktop-connector"`) keeps the historical
service name, so existing user keyrings keep working without
migration. Non-default config dirs (the harness's `…-dev`, any
power-user multi-profile setup) get their own service slot. The
`DC_KEYRING_SERVICE` env var is still honoured as a global override.
All four free functions (`open_default_grant_store`,
`local_vault_grant_exists`, `delete_local_grant_artifacts`, plus the
disconnect path's direct `KeyringGrantStore.open_default()` call)
thread the resolved service through. The leaked dev grant from
test 04 was migrated out of `desktop-connector` into
`desktop-connector-dev` by hand once the fix was in place.

**Alternatives.** (a) Skip the keyring entirely on non-default
config dirs and force the file fallback — simpler, but loses keyring
benefits (auto-locking on screen lock, GNOME Keyring's per-app
visibility) for legitimate multi-profile setups. (b) Take a
`SecretStore` from `Config` and reuse it instead of opening an
independent backend — cleaner long-term, but a bigger refactor (the
two stores have different value shapes today, plus
`vault_grant` ships a file fallback that `Config`'s store does not).
(c) The chosen fix — minimal symmetry with the existing per-config
keyring derivation in `Config`, no migration required for canonical
installs.

**Anchor.** `desktop/src/vault_grant.py`: `_DEFAULT_KEYRING_SERVICE`,
`_resolve_keyring_service`, `KeyringGrantStore.__init__` /
`open_default(service_name=…)`, the `service` argument threaded
through `open_default_grant_store`, `local_vault_grant_exists`,
`delete_local_grant_artifacts`. Tests:
`tests/protocol/test_desktop_vault_grant.py`
`GrantStoreKeyringServiceIsolationTests` (4 tests).

### 2026-05-06 — File-manager scripts carry a config-id marker for cross-install isolation

**Status:** accepted.

**Context.** `~/.local/share/nautilus/scripts/`, `~/.local/share/nemo/scripts/`,
and `~/.local/share/kservices5/ServiceMenus/` are per-user XDG paths
shared across **all** Desktop Connector installs on a host. Vault
automation suite 0002 test 02 launched a dev twin
(`--config-dir=~/.config/desktop-connector-dev`, no pairings); on
startup the twin's `sync_file_manager_targets` call iterated the shared
Nautilus dir, treated the canonical install's "Send to Vivo Phone"
managed script as stale (because its peer wasn't in the dev twin's
empty pair list), and unlinked it. Same shape as the 2026-05-06
keyring-isolation bug (`Config` now derives the keyring service name
from `config_dir.name` to fix that one) but on a different shared
resource. Per `feedback_test_isolation.md` the rule is: shared-resource
isolation must live in the code path, not in shell discipline.

**Decision.** Every managed file-manager entry now embeds a
`# desktop-connector:config-id=<config_dir.name>` marker alongside the
existing `MANAGED_SENTINEL` and `PAIRING_ID_PREFIX`. Both the cleanup
pass and the write-collision check honour ownership: a managed entry
whose marker doesn't match the current `config_dir.name` is left alone
(even if it would otherwise look stale), and the write pass refuses to
clobber such an entry with `skip_other_config_collision`. Pre-fix
unmarked managed entries (and unmarked legacy "Send to Phone" scripts)
are treated as canonical-owned: only the canonical install
(`config_dir.name == "desktop-connector"`, the XDG default) adopts and
rewrites them with the marker on first sync; alternate-config installs
leave them untouched.

**Alternatives.** (a) Skip `sync_file_manager_targets` entirely on
non-default config dirs — one-line change, but loses multi-profile
support for power users running e.g. AppImage + dev-tree side by side
with their own paired phones. (b) Add a `--no-file-manager-sync` flag
used only by the harness — same shape as the
`DC_KEYRING_SERVICE` mistake the rule above warns against (easy to
forget, leaves shared-state damage as the failure mode). (c) The
config-id marker — chosen — costs one comment line per script and
keeps multi-profile working correctly.

**Anchor.** `desktop/src/file_manager_integration.py`:
`CONFIG_ID_PREFIX`, `_config_marker`, `_owns`, `_extract_config_id`;
the cleanup ownership gate and the collision refusal in
`_sync_script_dir` and `_sync_dolphin_service`. Tests:
`tests/protocol/test_desktop_file_manager_integration.py`
`FileManagerCrossConfigIsolationTests`.

### 2026-05-06 — Folders tab dispatches via a VaultRuntime, not raw Vault.* calls

**Status:** accepted.

**Context.** The Vault settings Folders tab spawned worker threads
that opened the local vault, called `Vault.add_remote_folder` /
`Vault.rename_remote_folder` / `flush_and_sync_binding` directly,
and threaded a per-tab `threading.Lock` through every callsite.
The tab thus mixed three concerns: GTK widget assembly, worker
thread plumbing, and vault-mutation business logic. F-517's lock
made it correct; F-518 needed to make it readable + testable
without GTK on the path.

**Decision.** Introduced `VaultRuntime` (`desktop/src/vault_folder_runtime.py`)
— a small GTK-free object that holds the per-tab serialization
lock, opens and closes the local vault around each operation, and
exposes named ops (`fetch_manifest`, `add_remote_folder`,
`rename_remote_folder`, `flush_and_sync_binding`,
`run_initial_baseline`). The tab keeps owning GTK widget mutation,
`threading.Thread` spawning, and `GLib.idle_add` result forwarding;
the runtime owns the vault lifecycle. The runtime takes `opener` +
`relay_factory` injection points so its tests don't bring up real
crypto / keyring / relay.

**Alternatives.** Keeping the raw inline calls (rejected: the tab
was 644 lines of mixed concerns, and any future tab — Devices,
Activity, Maintenance — would have to re-derive the
`open_local_vault_from_grant` + lock + `close` pattern from
scratch). A queue-based async runtime where the tab posts ops and
listens for completions (rejected: synchronous "open → run → close
under the lock" matches GTK's worker-thread + `GLib.idle_add`
model exactly; queueing would invert control without buying any
new property over what the lock already gives us). Pushing the
lock into `Vault` itself (rejected: the lock is a tab-level
concern — overlapping clicks within one Folders tab — not a
process-wide vault invariant).

**Anchor.** `desktop/src/vault_folder_runtime.py` (the runtime),
`desktop/src/vault_folders_tab.py` (the now-thinner tab),
`tests/protocol/test_desktop_vault_folder_runtime.py` (lifecycle +
serialization + source pins). Commit `3a3c69b`.

### 2026-05-06 — Vault subprocess windows accept --vault-id

**Status:** accepted.

**Context.** Every vault GTK window (`vault-main`, `vault-browser`,
`vault-import`) read its active vault id from
`config['vault']['last_known_id']` on disk. That ties the
subprocess identity to whatever is most recently in config —
fine for the single-vault tray we ship today, but it closes the
door on multi-vault routing (future tray with N vaults; smoke-test
drivers; concurrent wizards). Repointing a window at a specific
vault would have meant rewriting config out of band, which races
with the wizard subprocess.

**Decision.** The subprocess dispatcher (`desktop/src/windows.py`)
accepts an optional `--vault-id` arg and threads the normalized
form into every `show_vault_*` entry point that has a vault
context. Each window's `local_vault_id()` closure delegates to a
new `vault_window_args.resolve_active_vault_id(config, override)`
helper: explicit override wins; otherwise reads `last_known_id`
after a fresh `config.reload()`. `parse_vault_id_arg` validates
strictly (RFC 4648 base32, 12 chars, accepts dashed/lowercase) and
surfaces malformed input as a clean `parser.error` so the
subprocess fails fast instead of silently routing to the
"no vault opened" placeholder. `vault-onboard` and
`vault-passphrase-generator` deliberately do *not* receive the
arg — onboard creates a new vault (override would contradict),
passphrase-generator has no vault context.

**Alternatives.** An env var (rejected: less discoverable; argparse
gives validation + `--help` for free). A file path (rejected:
more state for the subprocess to read at startup, and config
already exists for the fallback path). Keeping config-only routing
(rejected: forces every future caller into a config-rewrite race
with the wizard).

**Anchor.** `desktop/src/vault_window_args.py` (parser + resolver,
GTK-free), `desktop/src/windows.py` (dispatcher + arg validation),
the three `show_vault_*` callsites in `windows_vault.py` /
`windows_vault_browser.py` / `windows_vault_import.py`,
`tests/protocol/test_desktop_vault_window_args.py`. Commit `7f1e88e`.

### 2026-05-11 — Vault autosync is connection-gated, kicked on reconnect, slower

**Status:** accepted.

**Context.** A battery-usage dump from a paired phone showed Desktop
Connector as the #2 power consumer (109 mAh / 4h 50m) with 95 % of
that drain on `mobile_radio` and 80 `FcmService` launches in the
window. The desktop log over the same period showed
`connection.backoff.retry` storms driven by transient
`[Errno 101] Network is unreachable` failures (78 in 37 hours,
spiking to 22 in two hours during a wifi-unstable stretch). Two
desktop-side amplifiers were stacking on top of the underlying
local-network flakiness:

1. `VAULT_AUTOSYNC_INTERVAL_S = 15.0` had the tray-side autosync
   loop firing `flush_and_sync_binding` four times per minute even
   when nothing changed. Each pass does a manifest fetch; during a
   flaky window every one of them tripped the backoff path. Over
   37 hours the log carried 4000 `vault.sync.autosync.tick` events.
2. `icon_poll`'s reconnect handler called `_maybe_ping(0.0)` on
   every CONNECTED transition — so each transient drop forced a
   fresh HIGH-priority FCM ping wake on the phone, regardless of
   how recently we had pinged.

Watchers fire `_vault_autosync_kick` on real file changes via
inotify/FSEvents, so responsiveness to user edits never depended on
the periodic interval — only the no-op backstop cadence did. And
the catch-up filesystem scan inside `flush_and_sync_binding` plus
the watcher pending-ops queue both survive arbitrary gaps, so
skipping a pass costs at most the next-pass delay.

**Decision.** Three tightly coupled adjustments in
`desktop/src/tray/`:

1. `VAULT_AUTOSYNC_INTERVAL_S` 15 s → 60 s. Comfortably above the
   typical wifi-reassoc/DHCP-renew blip; still bounded enough that
   a missed watcher event recovers within a minute.
2. The autosync loop checks `self.conn.state == ConnectionState.CONNECTED`
   before doing any network work and `continue`s otherwise. Paired
   with a one-time `on_state_change` callback that sets
   `_vault_autosync_kick` on the next CONNECTED transition, so
   recovery is instant rather than waiting up to a full interval.
3. The reconnect ping in `icon_poll` uses `min_age = 30.0` (the same
   cache window as the menu-open ping) instead of `0.0`. Brief
   blips reuse the recent ping result; genuine multi-minute
   reconnects still ping fresh through the existing 5 min cache.

The combined effect: a wifi flap no longer cascades into a
vault-manifest retry storm AND a fresh phone FCM wake — both
amplifiers are damped.

**Alternatives.** Subscribe the autosync loop to network-up signals
via NetworkManager dbus (rejected: platform-specific, breaks
non-NM Linux distros and never works on Windows/macOS; the
connection-state callback we already have is portable and
sufficient). Dropping the reconnect ping entirely (rejected: real
"just opened the laptop after lunch" cases still want fast
feedback; the 30 s window keeps that path live while filtering
the sub-30 s blips). Keeping the 15 s interval and only adding
the connection gate (rejected: still produces a manifest fetch per
minute under normal conditions for an idle vault, which is pure
noise and slowly contributes to phone modem tail-energy via the
long-poll path even without retries).

**Anchor.** `desktop/src/tray/vault_submenu.py` (interval constant,
state-change subscription at first watcher start, connection gate
inside `_vault_autosync_loop`), `desktop/src/tray/app.py` (the
`min_age = 30.0` reconnect-ping change in `icon_poll`).

---

_(add new decisions above this section header)_
