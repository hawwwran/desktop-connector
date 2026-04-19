# desktop-hardening-plan.md

## Purpose

This document describes a practical hardening plan for the Desktop Connector desktop client.

The focus is on **local secret storage and desktop-side security posture**, not on transport encryption.

The current desktop client already has a strong in-transit model, but local at-rest handling of secrets is not yet strong enough for a more professional desktop application.

This plan explains:

- what is currently stored,
- what is weak about the current design,
- what should be hardened first,
- how to preserve a smooth migration path,
- and how to keep compatibility during a transition period.

---

## Current local-secret situation

Today, the desktop client stores highly sensitive material in the config directory.

### Current storage locations

#### Config file
`~/.config/desktop-connector/config.json`

This currently stores:
- `device_id`
- `auth_token`
- `paired_devices`
- for each paired device:
  - `pubkey`
  - `symmetric_key_b64`
  - `name`
  - `paired_at`

#### Private key
`~/.config/desktop-connector/keys/private_key.pem`

This stores the long-term device private key.

### What is currently good
The key directory is explicitly created with restrictive permissions and the private key file is explicitly written with restrictive permissions.

### What is currently weak
The config file currently holds:
- the server auth token
- pairing-derived symmetric keys

and it is stored as ordinary JSON rather than in secure secret storage.

That means the app is strong on the wire, but weaker than it should be for local secret storage.

---

## Security objective

The objective is not “perfect secrecy against a fully compromised local account.”

The realistic objective is:

- improve local secret handling significantly,
- reduce accidental secret exposure,
- align the app with normal desktop secret-storage expectations,
- keep migration practical,
- and avoid breaking development workflows unnecessarily.

---

## Main hardening direction

The long-term target should be:

### Store secrets in the OS secret store
On Linux, the preferred direction is to use the system secret-storage layer rather than plain JSON config files.

A good Linux-oriented target is the Secret Service ecosystem, which is exposed through libsecret on GNOME-family systems. Libsecret’s simple password API is explicitly intended for storing and retrieving secrets, and its stored item attributes are *not* secure and should not contain secret values.

If the future Qt client wants a Qt-friendly abstraction, QtKeychain is designed exactly for secure secret storage behind Qt, using Linux secret stores such as GNOME Keyring / libsecret and KWallet where available, and it explicitly states that unsupported environments do not silently fall back to plaintext unless insecure fallback is enabled.

### Keep non-secret settings in config.json
The JSON config file should remain for ordinary settings such as:
- server URL
- save directory
- UI preferences
- device display name
- optional logging preference
- non-secret metadata

This gives a cleaner split:
- config file for settings
- secret store for secrets

---

## What should be treated as secrets

The following should be treated as secrets and moved out of plain JSON:

### 1. `auth_token`
This grants authenticated access as the desktop device.

### 2. pairing-derived symmetric keys
The `symmetric_key_b64` values are the most important local secrets after the private key.

### 3. long-term private key
The long-term private key already has better filesystem protection than the JSON config, but it should still be considered part of the hardening scope.

The app may choose between:
- continuing to store it on disk with strong permissions,
- or moving it into secret storage if the platform strategy supports that cleanly.

This does not need to be the first migration step, but it should remain in scope.

---

## What should not be treated as secrets

The following should generally remain outside secret storage:

- `device_id`
- paired device public keys
- device names
- `paired_at`
- save directory
- server URL
- UI preferences

These may still be sensitive in a privacy sense, but they are not secret material in the same way as tokens and symmetric keys.

Also note that secret-store lookup attributes should never themselves contain secret material.

---

## Recommended target storage model

## Config file should keep:
- `server_url`
- `save_directory`
- `device_name`
- `auto_open_links`
- `allow_logging`
- pairing metadata without secret values
- migration markers / storage version markers

## Secret storage should keep:
- `auth_token`
- per-device pairing symmetric keys
- optionally later the long-term private key or an encrypted wrapper around it

A good resulting model is:

### `config.json`
Contains only non-secret configuration and metadata.

### Secret store entries
Contain:
- desktop auth token
- pairing secret material per paired device

### Private key
Initially may remain on disk with strict permissions, then be revisited later.

---

## Shared-data compatibility requirement

A very important goal for the migration should be:

**allow the existing Python client and an alternative Qt client to coexist at the data-model level during development, as long as they are not run at the same time.**

This matters because it dramatically lowers migration friction.

A successful hardening plan should support:

- stop Python app
- start Qt app
- reuse the same identity and pairing state
- stop Qt app
- start Python app again if needed

This should be treated as a first-class migration requirement.

---

## Recommendation for migration compatibility

## Short-term compatibility model
During the transition period, the Qt client should be able to read:

- the current `config.json`
- the current `keys/private_key.pem`

and interpret the existing pairing layout.

That means the Qt client should be backward-compatible with the current on-disk format first.

### Why this is valuable
It allows:
- incremental migration
- side-by-side development
- easy rollback during testing
- lower conversion risk

---

## Medium-term compatibility model
Once hardening begins, the new client should support a **migration-aware storage layer**:

### Read order
1. try secure secret storage
2. if missing, fall back to legacy `config.json`
3. if legacy values are found, optionally import them into secure storage
4. mark migration status in config
5. optionally scrub legacy secret fields later

This gives a safe transition path.

---

## Long-term compatibility model
Eventually the preferred state should be:

- both clients understand the new storage model,
- legacy plaintext secret fields are no longer required,
- and plaintext secret storage is retired.

But this should happen only after the migration period is stable.

---

## Recommended hardening phases

## Phase 1 — lock down current filesystem behavior
Before changing storage architecture, improve the current file-permission story.

### Goals
- ensure config directory is created with restrictive permissions
- ensure `config.json` gets explicit secure permissions
- audit any other config-side files for permissions
- keep this change low-risk and backward-compatible

### Why first
This is a cheap improvement even before secret-store migration begins.

### Deliverables
- explicit permissions on config directory
- explicit permissions on config file
- permission-check/logging for insecure existing files where useful

---

## Phase 2 — separate secret vs non-secret data model
Refactor the config model so the app clearly distinguishes:

- settings
- metadata
- secrets

### Goals
- stop treating one JSON blob as the storage location for everything
- introduce a secret-storage interface
- define exactly which values move to secret storage

### Deliverables
- secret storage abstraction
- reduced config schema
- migration marker/version field

---

## Phase 3 — add secret store backend
Implement a Linux secret-storage backend.

### Preferred Linux direction
Use a secret storage backend aligned with the Linux desktop secret ecosystem.

Two viable directions are:

#### Option A — libsecret-based integration
A Linux-native route aligned with Secret Service / GNOME-style secret storage.

#### Option B — QtKeychain for the Qt client
A Qt-oriented route that uses Linux desktop secret stores and is better aligned with a Qt desktop shell. QtKeychain explicitly supports Linux secret backends and avoids insecure fallback unless explicitly enabled.

### Deliverables
- secure secret read/write
- lookup strategy by non-secret attributes
- no insecure fallback by default

---

## Phase 4 — add legacy import logic
Make the new secret layer import from legacy config if needed.

### Goals
- preserve compatibility with existing installs
- support Python -> Qt transition
- support staged rollout without forcing immediate conversion

### Deliverables
- legacy reader
- import-on-first-use or explicit migration command
- migration status marker

---

## Phase 5 — split pairing metadata from pairing secrets
Refactor pairing storage so that:

### In config
Store only:
- device ID
- public key
- display name
- timestamps
- maybe secret-store lookup key or alias if needed

### In secret storage
Store:
- the pairing symmetric key

This is the most important practical hardening step after auth token migration.

---

## Phase 6 — harden auth token storage
Move the server auth token into secure storage as well.

### Why
If someone can read the auth token, they may act as the desktop device against the relay.

### Deliverables
- auth token stored in secret storage
- config only stores metadata needed to locate the token if necessary

---

## Phase 7 — revisit long-term private key storage
After the rest of the migration is stable, decide whether the private key should:

- remain as a strictly permissioned filesystem secret
- be stored in the secret store
- or be encrypted locally with a key derived from secret-store-protected material

This is a more delicate step and should come later.

### Recommendation
Do not make this the first migration step.  
First remove the easy-to-expose secrets from plain JSON.

---

## Phase 8 — optional secure cleanup of legacy plaintext
After migration has been stable for some time, add a cleanup path that removes legacy plaintext secret fields from `config.json`.

### Important note
Do not do this too early.

Keep rollback and dev compatibility simple until the new client path is stable.

---

## Qt client compatibility answer

## Can the Qt app reuse the same pairing data initially?
**Yes — and it should, if you want the migration to stay practical.**

If the Python app is closed and the Qt app is then started, the Qt app can absolutely be designed to use the same current desktop data:

- same `config.json`
- same `keys/private_key.pem`
- same pairing metadata shape
- same pairing symmetric key entries
- same auth token

That is the best development-time migration strategy.

### Why this is a good idea
It gives you:
- easier iterative development
- no repeated re-pairing
- lower migration friction
- easier comparison between old and new desktop clients
- easier rollback if something breaks

---

## Important caveat: do not run them simultaneously
This is the critical warning.

The current desktop config model does not appear to have the same cross-process locking discipline for `config.json` that the history file has for `history.json`.

That means sharing the same config is reasonable for:
- one app stopped
- the other app started

but not ideal for:
- both running at the same time
- both writing config state opportunistically

### Recommended rule
Treat the current shared config as:
- **safe enough for sequential development use**
- **not a good concurrent multi-client setup**

That should be the explicit development rule.

---

## Recommended transition policy for your dev workflow

### Development stage 1
Qt client reads the exact same current config and key material as the Python client.

Policy:
- never run both at once
- stop one before starting the other

### Development stage 2
Qt client introduces secret-store support but still understands legacy config.

Policy:
- read secure storage first
- fall back to legacy config
- optionally import legacy secrets into secure storage

### Development stage 3
Both clients can support the new storage layout.

Policy:
- one shared identity
- one shared pairing set
- no repeated pairing required
- old plaintext secret fields gradually phased out

This is the smoothest migration path.

---

## Recommended implementation rule

The new storage layer should be built around this principle:

**identity and pairing state are conceptually shared, but secret material should be abstracted behind a storage interface.**

That means the Qt app should not hard-code:
- “read `symmetric_key_b64` from JSON forever”

It should instead do:
1. ask secret storage for pairing secret
2. if missing, check legacy JSON
3. migrate if appropriate

This makes the transition easy without freezing the old insecure model permanently.

---

## What not to do

## 1. Do not break existing installs immediately
Forcing all users into a new storage model in one step is unnecessary risk.

## 2. Do not require re-pairing just because the desktop shell changed
If the app identity is conceptually the same device, re-pairing should be avoided where possible.

## 3. Do not run both desktop clients against the same mutable config concurrently
That is asking for subtle state corruption or divergence.

## 4. Do not store secret-store lookup attributes as secrets
Secret-store attributes are identifiers, not secret data.

## 5. Do not enable insecure plaintext fallback silently
If a secret backend is unavailable, the behavior should be explicit and intentional.

---

## Recommended immediate actions

If you want a sensible near-term hardening sequence, do this:

### Immediate
1. enforce restrictive permissions on config dir and config file
2. define a secret-storage abstraction
3. keep the current on-disk format readable by both clients
4. forbid concurrent Python + Qt client usage during development

### Next
5. move `auth_token` and pairing symmetric keys into secret storage
6. keep pairing metadata in JSON
7. add legacy import logic

### Later
8. revisit private key storage
9. remove legacy plaintext secret fields once migration is proven safe

---

## Practical definition of success

This plan is successful if, after implementation:

- the desktop app no longer stores auth token and pairing symmetric keys in plain JSON
- the Qt client can still reuse existing desktop identity/pairing data during migration
- developers can switch between Python and Qt clients without re-pairing
- sequential use is supported cleanly
- concurrent use is explicitly discouraged or prevented
- local secret handling is significantly improved without causing migration chaos

That is the right goal.
