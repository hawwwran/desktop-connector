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

---

_(add new decisions above this section header)_
