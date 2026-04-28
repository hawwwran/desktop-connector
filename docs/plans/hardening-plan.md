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

### H.5 — Headless / no-Secret-Service handling ⏱ ~45 min

Make the no-keyring case explicit.

**What changes:**
1. `Config.__init__` detects whether `SecretServiceStore` could
   open. If not, check `_data.get("allow_plaintext_secrets")` or
   the `--allow-plaintext-secrets` CLI flag (added to
   `bootstrap/args.py`).
2. With opt-in: instantiate `JsonFallbackStore` and log a
   `WARNING` per logging vocabulary
   (`config.secrets.plaintext_fallback`).
3. Without opt-in: print a one-line error to stderr and
   `sys.exit(1)`. The error mentions both the flag and the
   config field.
4. The receiver-mode default install (`install-from-source.sh` /
   AppImage first-launch) should not enable the flag — only
   headless deployments where the operator chose this trade-off.

**Acceptance:**
- Stop GNOME Keyring → Python client fails to start with the
  one-line error mentioning the override
- Same with `--allow-plaintext-secrets` → starts, logs the
  warning, secrets land in `config.json` with `chmod 0600`
- The flag's presence is logged on every start (so a long-running
  headless instance leaves a paper trail)

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

### H.7 — Revisit private_key.pem (deferred) ⏱ ~unknown

The long-term private key already has good filesystem protection
(written `chmod 0600` at creation by `crypto.py`). Whether to
also move it into the secret store is a separate trade-off:

- Pro: encryption-at-rest gated by session unlock; lifetime
  matches the rest of the secret material.
- Con: the secret store stores strings (base64-encode the PEM);
  rotation gets harder; libsecret's per-secret size limits vary
  per backend.

Defer until H.1–H.6 are stable and there's a felt need. Until
then the on-disk key with `chmod 0600` is acceptable.

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
