# Desktop hardening plan — at-rest secret storage

**Status: DRAFT — not started.** Implementation plan for moving
the desktop client's at-rest secrets out of plaintext JSON into
the OS Secret Service (libsecret on GNOME-family / GNOME Keyring,
KWallet on KDE).

This plan replaces the earlier `hardening-plan.md` draft (which
assumed a parallel Qt client was migrating; that target was
dropped when AppImage replaced the Qt cutover — see
`temp/finished-plans/desktop-client-migration-plan.md`). With a
single Python client, the migration story collapses to "rotate
config layout in place, on the same client".

---

## Why

The desktop client is strong on the wire (X25519 + AES-256-GCM)
but stores everything at rest in `~/.config/desktop-connector/`
as plain JSON or a permissioned PEM:

| File | Contents | Sensitivity |
|------|----------|-------------|
| `config.json` | `device_id`, **`auth_token`**, server URL, save dir, `paired_devices[].pubkey`, **`paired_devices[].symmetric_key_b64`**, `name`, `paired_at`, UI prefs | High — auth_token + symmetric keys live here |
| `keys/private_key.pem` | long-term X25519 device private key | High — but already chmod-protected at write time |
| `history.json` | last 50 transfer records (filenames, timestamps; no key material) | Low |
| `logs/*.log` (opt-in) | event vocabulary; no key material per logging policy | Low |

A plaintext `auth_token` lets anyone with read access to the
home directory act as the desktop device against the relay. A
plaintext `symmetric_key_b64` per paired device lets them decrypt
intercepted ciphertext (assuming they also recover the relay's
encrypted blobs — but we shouldn't rely on the relay being
unreachable as the only barrier).

The realistic goal here is **not** "perfect secrecy against a
fully compromised local account". It's "match normal desktop
secret-storage expectations" — encryption-at-rest gated by the
desktop session unlock.

## Current code surface

- `desktop/src/config.py:210` — `auth_token` getter/setter on
  the JSON dict
- `desktop/src/config.py:239` — `add_paired_device` writes
  `symmetric_key_b64` into the JSON dict
- `desktop/src/config.py:263` — `clear_pairings` modes
  (`pairing_only` / `full`); both touch the JSON dict
- `desktop/src/api_client.py` — reads `auth_token` for every
  authenticated request
- `desktop/src/runners/registration.py`, `runners/pairing.py` —
  write secrets via the config object

Any plan that moves secrets out of JSON has to thread through
those callers without breaking them.

---

## Backend choice (deferred to H.3)

Three viable Python integrations with the Linux Secret Service:

| Option | Pros | Cons |
|--------|------|------|
| `keyring` (PyPI) | clean abstraction; widely tested | adds dep + jeepney; no native attribute lookup |
| `secretstorage` (PyPI) | direct Secret Service API; jeepney-based | Linux-only (fine, we already are); slightly more API surface |
| `gi.repository.Secret` | already bundled via GTK4 stack — zero new deps | most verbose API |

Recommendation: **`keyring`** for the cleanest call sites
(`keyring.set_password("desktop-connector-pairing-symkey", device_id, key)`),
unless the AppImage bundle audit (H.3) shows it pulls in
problematic transitive deps. Final pick belongs in H.3 — don't
prescribe before validating against the bundle.

## Headless mode is a real constraint

The desktop client also runs `--headless` (no GUI, just
receiver). On a headless server there is typically no D-Bus
session bus and no Secret Service running. The plan **must**
handle this — silently writing plaintext when the secret store
is unreachable is exactly the "insecure fallback" failure mode
the original plan called out.

Approach (formalised in H.5):
- Probe Secret Service availability at startup.
- If reachable → use it.
- If not reachable AND `allow_plaintext_secrets: true` is set
  in `config.json` (or `--allow-plaintext-secrets` CLI flag is
  passed) → fall back to plaintext, with chmod 0600 + a logged
  warning on every start.
- Otherwise → exit 1 with a clear one-line error pointing at
  the override flag and explaining the trade-off.

This makes the insecure path explicit, opt-in, and noisy.

---

## Phases

Each phase is independently landable and ends on a single
commit. Estimates assume the AppImage build path stays green
and the protocol contract tests still pass.

### H.1 — Lock down filesystem permissions ⏱ ~30 min

Before any architecture change, do the cheapest improvement:
make the existing files less readable.

**What changes:**
1. `Config.__init__` (or wherever the config dir is created):
   `os.makedirs(DEFAULT_CONFIG_DIR, mode=0o700, exist_ok=True)`
   followed by `os.chmod(DEFAULT_CONFIG_DIR, 0o700)` to fix any
   existing dirs that were created with weaker permissions.
2. `Config.save()`: write to a tmp file, `os.chmod(tmp, 0o600)`,
   then `os.replace(tmp, target)` (atomic + permissioned).
3. `Config.load()`: log a warning if the existing file is
   world-readable / group-readable, then fix on next save.
4. Same treatment for `history.json`.

**Acceptance:**
- `stat -c '%a' ~/.config/desktop-connector/` returns `700`
- `stat -c '%a' ~/.config/desktop-connector/config.json`
  returns `600`
- Existing installs fix their own permissions on next save
- `test_loop.sh` still green

### H.2 — Secret-storage abstraction in code ⏱ ~1 h

Refactor `config.py` so secret reads/writes go through a small
interface. **No backend yet** — the only implementation in this
phase is "store in config.json", byte-equivalent to today.

**What changes:**
1. New `desktop/src/secrets.py` exposing:
   ```python
   class SecretStore(Protocol):
       def get(self, key: str) -> str | None: ...
       def set(self, key: str, value: str) -> None: ...
       def delete(self, key: str) -> None: ...
       def is_secure(self) -> bool: ...
   ```
2. `JsonFallbackStore(config_path)` — current behaviour, keeps
   secrets inside `config.json`. `is_secure() → False`.
3. `Config` accepts a `SecretStore` (default = JsonFallbackStore).
   `auth_token` getter/setter and `add_paired_device` route
   through it instead of touching `_data` directly.
4. Define the canonical attribute scheme used by all backends:
   - `auth_token` → key `auth_token`
   - per-pairing symkey → key `pairing_symkey:<device_id>`

**Acceptance:**
- All callers of `auth_token` and `paired_devices` work unchanged
- Protocol contract tests pass
- A new unit test pins the abstraction (`SecretStore` get/set/delete
  round-trip)

### H.3 — Secret Service backend ⏱ ~2 h

Implement `SecretServiceStore` against libsecret.

**What changes:**
1. Audit AppImage bundle implications: pick `keyring` vs
   `secretstorage` vs `gi.repository.Secret` based on what the
   AppImage already pulls in cleanly. Document the choice in
   `secrets.py`.
2. `SecretServiceStore` class implementing the `SecretStore`
   protocol. Service identifier: `"desktop-connector"`. Lookup
   attribute: `{"category": "auth_token"}` or
   `{"category": "pairing_symkey", "device_id": "<id>"}`.
3. Probe at process start: try opening the keyring; on
   `SecretServiceUnavailable` / `dbus.exceptions.DBusException`,
   surface a typed exception that callers can branch on.
4. `is_secure() → True`.

**Acceptance:**
- Fresh install on a GNOME / KDE machine writes secrets to the
  keyring; `seahorse` (or `kwalletmanager`) shows them under
  "desktop-connector"
- AppImage size doesn't grow by more than ~1 MiB
- Toggling lock state of the keyring blocks reads with a
  clear typed error (no plaintext fallback on accident)

### H.4 — Migration: import legacy plaintext on first run ⏱ ~1 h

When the new code first sees an old-style `config.json` that
still has `auth_token` / `symmetric_key_b64` inline, copy them
into the secret store and write a migration marker.

**What changes:**
1. On `Config.load()`, after secret store initialisation:
   - If `_data` contains `auth_token` and the secret store is
     secure: copy to secret store, remove from `_data`, set
     `_data["secrets_migrated_at"] = "<iso8601>"`, save.
   - Same for each `paired_devices[*].symmetric_key_b64`.
2. If the secret store is **not** secure (H.5 fallback path),
   leave the legacy fields in place and **do not** add the
   migration marker — the next more-secure boot will pick up
   the migration.
3. `Config.clear_pairings("full")` and the auth-failure recovery
   path (`docs/diagnostics.events.md` "auth_failure_kind") must
   delete from the secret store, not just the JSON dict.

**Acceptance:**
- Existing install with plaintext secrets boots, secrets land in
  the keyring, `cat ~/.config/desktop-connector/config.json`
  shows no `auth_token` or `symmetric_key_b64` afterwards
- `clear_pairings("full")` after migration leaves no orphaned
  keyring entries (verified via `seahorse` or programmatic list)
- Re-pairing after migration writes new entries to the keyring,
  not to `config.json`

### H.5 — Surface the no-keyring fallback to the user ⏱ ~45 min

Make the JSON-fallback path **visible** rather than blocking.
H.4 already falls back silently when the Secret Service can't be
reached; H.5 turns that silence into two visible surfaces — a
stderr warning for CLI runs and a clickable tray menu indicator
for desktop-mode runs — so the user knows their secrets are
sitting in plaintext config.json and what to do about it.

**Policy shift from the prior draft:** the original H.5 was
going to refuse to start without an explicit
`--allow-plaintext-secrets` opt-in. That's too aggressive for
this project's audience (single-user desktops, occasional
headless servers); a refused-startup leaves the user with no
clear recovery path on a phone with active pairings. The new
posture is **warn-but-don't-block**: the desktop keeps
working, but the warning is loud enough to be hard to ignore.

**What changes:**

1. `Config.is_secret_storage_secure()` public method (thin
   wrapper around the existing `self._secret_store.is_secure()`)
   — single canonical surface for the "is fallback active?" check.
2. CLI / headless mode: at startup, if not secure, print a
   single-line warning to stderr (e.g., "⚠ Secret Service
   unavailable — auth_token and pairing keys stored in plaintext
   ~/.config/desktop-connector/config.json. Install
   gnome-keyring / kwallet to fix.") in addition to the existing
   `config.secrets.fallback_to_json` log event. No exit, no opt-in
   flag required. Re-emitted once per process start.
3. Tray-mode warning row: a new `pystray.MenuItem` rendered
   directly under the connection-status row when not secure.
   Label format mirrors the existing warning rows ("⚠ Secrets in
   plaintext — click for info"). Click handler spawns a new GTK4
   subprocess window via the existing `_open_gtk4_window` /
   `--gtk-window=` dispatch.
4. Explainer window: new
   `show_secret_storage_warning(config_dir)` in
   `desktop/src/windows.py`. Adw status-page layout with three
   sections — what's happening (plaintext storage), why (no
   reachable Secret Service backend), how to fix (typical
   commands per desktop / how to install gnome-keyring on
   Zorin/Ubuntu, KWallet on KDE, etc.). Single "Close" button.
   Registered in the windows.py `--gtk-window=` choices list.
5. Diagnostic event: `config.secrets.user_warned` emitted on
   each surface (one per CLI start, one per tray-menu open
   while in fallback mode) so logs show how many times the user
   has been told.

**What does NOT change:**
- No CLI flag, no config field. The fallback is always
  permitted; we just make it noisy.
- No startup blocking. Even fully headless deployments without
  a Secret Service keep working — just with the warning logged
  every boot.
- Migration semantics from H.4 unchanged. If keyring becomes
  available on a later boot, H.4's migration runs then.

**Acceptance:**
- Stop GNOME Keyring → next desktop launch starts cleanly,
  emits `config.secrets.fallback_to_json` and
  `config.secrets.user_warned` to log + stderr, tray menu shows
  a warning row directly under the connection status.
- Click the tray warning row → explainer window opens with
  what/why/how-to-fix sections.
- `--headless --send=…` mode → stderr warning prints once
  before the send proceeds; transfer still completes.
- Restart GNOME Keyring + restart desktop → warning row
  disappears, secrets get migrated by H.4 on this boot, log
  emits `config.secrets.using_keyring` instead.

### H.6 — Plaintext scrub command ⏱ ~30 min

After a stable migration, give the user a way to verify the
config file is clean.

**What changes:**
1. New CLI subcommand: `python -m src.main --scrub-secrets`.
   Loads config, verifies all known secret keys round-trip
   through the secret store, then explicitly removes any
   surviving legacy fields and re-saves with `chmod 0600`.
2. Surfaces a Settings-window button "Verify secret storage"
   that runs the same logic and shows the result.

**Acceptance:**
- `--scrub-secrets` on a freshly-migrated install reports
  "no legacy fields found"
- `--scrub-secrets` on an install where the user hand-edited
  `auth_token` back into JSON: removes it again
- Settings button shows the same outcome with a brand-styled
  status row

### H.7 — Move private_key.pem into the secret store ⏱ ~2 h

**Status: DONE.** The long-term X25519 private key now rides on
the same `SecretStore` backend as `auth_token` and pairing
symkeys. PEM file remains the storage of record only when no
secret service is reachable (headless / no-keyring deployments).

**What landed:**
- `crypto.KeyManager` accepts `secret_store: SecretStore | None`.
  Production callers pass `config.secret_store` (the same store
  Config picked via `open_default_store`); legacy callers / tests
  that pass nothing get the pre-H.7 PEM-only behaviour
  byte-for-byte.
- One-shot migration on init: existing `keys/private_key.pem`
  gets read, copied into the keyring under
  `private_key:pem`, and the file is deleted. Idempotent
  across re-inits (post-migration boots find the keyring
  authoritative and short-circuit). Partial-failure-safe (if
  `keyring.set_password` raises mid-migration, the PEM stays
  on disk for the next boot to retry — same shape as H.4).
- Stale-PEM cleanup: if the keyring already holds the live key
  and a PEM file appears (typically after a backup restore),
  the file is removed defensively so future loads can't pick
  up two competing sources of truth.
- Corrupt keyring entry refuses to silently regenerate — the
  operator gets a typed exception so they can triage via
  seahorse rather than losing the device identity to a parse
  error.
- `KeyManager.scrub_private_key()` covers the long-running
  process / Settings → Verify scenario where a PEM appears
  after init's migration check has already run. Returns True
  iff a migration occurred.
- `KeyManager.was_pem_migrated` flag lets `--scrub-secrets`
  CLI and the Settings Verify button surface "device private
  key" alongside the auth_token / symkey counts.
- `reset_keys()` wipes from BOTH backends — without that, a
  reset followed by a fresh-install would silently inherit
  the old keyring entry.
- `Config.secret_store` property exposes the active backend
  so production callers don't reach into `Config._secret_store`.

**Surfaces refreshed:**
- Settings → Security row subtitle now says "Identity, auth
  token + pairing keys" (was just "Auth token + pairing keys").
- The H.5 explainer window's "What's happening" section now
  enumerates `keys/private_key.pem` alongside `config.json`.
- The `--scrub-secrets` CLI line and Settings Verify message
  combine config-side counts with "device private key" into a
  single summary.

**Recovery posture:** post-migration, if the keyring entry is
lost (DB corruption, fresh OS install without restoring the
keyring, manual deletion via seahorse), the desktop generates a
fresh keypair on next launch — new device identity. The phone
will then need to re-pair. Accepted trade-off given this
project's audience (single-user sideload), and structurally
identical to the H.4 recovery posture for `auth_token` /
pairing symkeys: keyring loss = re-register / re-pair.

The same effect can be triggered intentionally via the tray's
"Re-pair" banner (visible during AUTH_INVALID auth-failure) or
the `local_unpair("full")` path in poller.py — both call
`crypto.reset_keys()`, which post-H.7 wipes both the keyring
entry and any leftover PEM before regenerating.

Routine `Settings → Unpair` and inbound `.fn.unpair` messages
do **not** touch the private key — they only clear the
specific pairing's symkey + metadata, preserving the device
identity.

**Tests:** 20 unit tests in
`tests/protocol/test_desktop_keymanager.py` covering legacy
PEM-only compatibility, secure-store fresh install, migration
of existing PEM, idempotence across re-inits, partial-failure
PEM retention, identity preservation across migration,
stale-PEM cleanup, scrub variants, store-read failure
fallback, and corrupt-keyring-entry refusal.

---

## What this plan does NOT do

- **No re-pairing.** Existing pairings survive the migration —
  the keyring entries are derived from the same plaintext values
  the JSON held.
- **No protocol changes.** Server, Android, on-the-wire format
  unchanged. Only the desktop's at-rest layout shifts.
- **No history-file encryption.** `history.json` is filenames +
  timestamps + sizes; not key material. Out of scope.
- **No per-user passphrase prompt.** The Secret Service backend
  inherits the desktop session's unlock. Adding a separate
  passphrase is a different threat model (offline attacker with
  full disk access) and would need its own plan.
- **No Windows / macOS support.** The Linux-only stance from
  the wider project carries through. `keyring` would auto-pick
  the right backend on those platforms if we ever cross-compile,
  but that's not on the roadmap.

## Risks + mitigations

| Risk | Mitigation |
|------|-----------|
| User on a niche desktop (sway, hyprland) has no Secret Service running. | H.5's opt-in plaintext fallback. The error message names common installs (`gnome-keyring`, `kwalletmanager`). |
| `keyring` / `secretstorage` adds D-Bus deps that bloat the AppImage. | H.3 audits before picking. Fall back to `gi.Secret` (already bundled) if size matters. |
| Migration partial-fails (keyring write succeeds, JSON remove fails). | H.4 runs as `set → verify-read → remove`, all in `Config.save()`'s atomic-rename pattern. |
| User locks the keyring mid-session. | Surface as a typed exception → auth-failure-kind banner already exists for "credentials wrong"; reuse the UX. |
| Secret-store entry visible to other processes via D-Bus. | Acceptable. Same-user processes already have access to the JSON file. The hardening upgrade is "encryption at rest", not "process isolation". |
| AppImage on a host without `python3-gi` for `gi.Secret`. | Bundle is self-contained; gi.Secret comes via the GTK4 plugin (already shipped for the windows). Verify in H.3 audit. |

---

## Practical definition of success

After H.1–H.6 land:

- `cat ~/.config/desktop-connector/config.json` shows no
  `auth_token` and no `symmetric_key_b64`
- `seahorse` (or equivalent) shows entries under
  `desktop-connector`
- A fresh install pairs successfully and never writes secrets
  to JSON
- An existing install upgrades transparently — no re-pairing
- Headless deployments either use the keyring (if available) or
  exit clean with an error pointing at the explicit opt-in
- `test_loop.sh` and `tests/protocol/` stay green
