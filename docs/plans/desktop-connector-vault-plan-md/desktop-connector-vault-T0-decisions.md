# Vault — T0 Decision Lock

This file freezes contracts referenced by the rest of the plan. **When plan files (01–11) and this document disagree, T0 decisions win.** Treat this as the canonical spec for the items below; amend deliberately, never silently.

Resolved during T0 review on 2026-05-02.

---

## Brand

The feature is **Vault** (or **Desktop Connector Vault** for the long form). The earlier working name "Tresor" is dropped throughout docs, code, identifiers, capability bits, event tags, and file extensions.

---

## D2 — Vault quota, warnings, and eviction policy

### Quota

- Each vault has a server-enforced ciphertext byte quota.
- **Default: 1 GB** (`1 073 741 824` bytes).
- Quota is **separate** from the existing transfer-pipeline quota. Vault chunks do not count against the per-device transfer cap and vice versa. Server schema: `vaults.quota_ciphertext_bytes`, distinct counter `vaults.used_ciphertext_bytes`.
- Configurable per-vault override on the relay is a **v1.5 follow-up** (relay config plumbed at T1 but no admin UI in v1).

### Pressure thresholds and UI surfacing

| Used | UI behavior |
|---|---|
| < 80 % | No indicator. |
| ≥ 80 % | Persistent warning bar in vault UI wherever it makes sense (header, settings → vault, browser breadcrumb). Not blocking. |
| ≥ 90 % | Same as 80 %, **plus** show the warning in the slot normally used for "update available" notices. **App-update notices take priority** in that slot — quota warning yields. |
| 100 % | Warning escalates to "Vault is full. Backup continuity may be compromised — oldest historical versions are being removed to keep current files syncing." Eviction policy below kicks in automatically. |

### Eviction policy (when ≥ 100 %)

When a write would exceed quota, the relay **rejects the write** with `vault_quota_exceeded`. Client responds by running an **eviction pass** in this strict order, retrying the write after each step:

1. **Hard-purge expired tombstones** — chunks whose `recoverable_until` has passed (see D5). Always safe; routine GC just brought forward.
2. **Hard-purge unexpired tombstones** — soft-deleted files whose retention has not expired yet, oldest-`deleted_at` first. **User is warned in the activity log**: `vault.eviction.tombstone_purged_early`.
3. **Hard-purge oldest historical versions of currently-live files** — for each file with > 1 version, drop the oldest version's chunks. Never drop the latest version. Activity log: `vault.eviction.version_purged`.
4. **No more eviction candidates → stop sync, prominent error.** New writes refused. Existing files remain readable. Client surfaces a top-level banner: "Vault is full and no backup history remains. Sync is stopped. Free space by deleting files, or export and migrate to a relay with more capacity."

Eviction is **automatic** (no per-event prompt) and is logged to the encrypted operation log; the user-visible warning at 100 % serves as the up-front consent. Steps 2 and 3 require **admin or sync** role on the device performing the operation. A read-only / browse-upload device cannot trigger eviction; if its write would force eviction it instead surfaces the 100 % banner without acting.

### Cross-references

- File 04 §"Server quota" — base quota field.
- File 04 §"Tombstone" — pairs with D5.
- File 08 §"Sync directions" — sync-stop path.
- File 09 — eviction threat-model implication: an attacker who can fill the vault can force shortened retention. Mitigation: eviction requires admin/sync role; read-only and browse-upload roles get refused on writes that would trigger eviction.
- File 10 — UI placement of warning bar; priority rule vs app-update notice.

---

## D3 — Device grants and exports

- Exports do **not** carry device grants.
- Importing an export creates a fresh vault state on the target relay (or merges into an existing one — see D9). The importing device receives a **new device grant with Admin role**, derived from the recovery material the user supplied during import.
- Other previously-paired devices need to be re-granted on the new relay via the QR-join flow, exactly as they would on a brand-new vault.
- Cross-ref: file 06 §"Export bundle structure" (remove `encrypted_device_grants` from the bundle); file 03 §"Device grants" (note import-time admin assignment).

---

## D4 — CAS merge algorithm

When `PUT /api/vaults/{id}/manifest` returns **409 Conflict** (current revision ≠ `expected_current_revision`), the client runs this algorithm:

1. Fetch current head `M[K]` (server returns full ciphertext + `current_revision = K`).
2. Decrypt `M[K]` and compute `Δ_remote = diff(M[N], M[K])` where `M[N]` is the parent the client was working from.
3. For each operation `op_i ∈ Δ_local` (the client's pending changes), apply the per-operation merge rule:

| Operation | Conflict trigger | Merge rule | Mode |
|---|---|---|---|
| Upload new file at path `P` | Another op in Δ_remote also created a file at `P` (different file_id) | Rename incoming to `P (imported)` and append `(imported N)` if collision recurses. | **auto** |
| Upload new version of existing file `F` | Δ_remote also added version(s) to `F` | Both versions land in `F.versions`. `latest_version_id` = max by `(timestamp, device_id_hash)`. | **auto** |
| Soft-delete file `F` | Δ_remote added a version to `F` | Tombstone wins. New version preserved as restorable history. | **auto** |
| Restore version `V` of file `F` | Δ_remote also restored or modified `F` | Last-write-wins by `(revision_number, device_id_hash)`. | **auto** |
| Rename file/folder | Δ_remote renamed the same target | Last-write-wins by `(revision_number, device_id_hash)`. Loser captured in op-log as historical name. | **auto** |
| Folder created | (none meaningful) | Independent — append. | **auto** |
| Folder soft-deleted | Δ_remote added entries to that folder | Folder tombstone wins; entries become restorable. | **auto** |
| Hard purge | Always | **Never auto-merge.** Re-prompt user (purge requires fresh-unlock anyway). | **manual** |
| Clear folder | Always | Equivalent to bulk soft-delete; auto-merge — chunks retained. | **auto** |

4. Build merged manifest `M' = apply(M[K], Δ_local')` where `Δ_local'` is the conflict-resolved version of `Δ_local`.
5. Set `M'.parent_revision = K`; `M'.revision = K + 1` (server validates and increments).
6. CAS-publish `PUT /api/vaults/{id}/manifest` with `expected_current_revision = K`.
7. On further 409, repeat from step 1.

### Tie-breaker

`(revision_number, device_id_hash)` lex-order — `device_id_hash` is the SHA-256 of the device id, taken big-endian. Two clients merging the same conflict converge to byte-identical manifests because the ordering is total.

### What "auto" means in UX

Auto-merge ops happen without a prompt. The user sees the merged result on their next manifest refresh and can review the activity log. Manual ops re-prompt with the latest server state in view.

### Cross-references

- File 05 §"Put manifest with CAS" — wire spec.
- File 07 §"CAS conflicts during browser operations" — uses this table.
- File 08 §"Upload sync flow" — uses this table.

---

## D5 — Tombstone retention semantics

- `recoverable_until = deleted_at + retention_policy.keep_deleted_days * 86400` (seconds).
- `deleted_at` is **client-provided** in the tombstone.
- `recoverable_until` is **computed server-side** during GC planning; the server uses **its own clock**, not the client's. This means a clock-skewed client cannot hide a delete by predating it, nor accidentally extend retention by post-dating it.
- `retention_policy` is **per remote folder**, set at folder creation, **immutable** after creation. Vault-default `keep_deleted_days = 30`.
- Changing retention for a folder is a **v1.5+** feature; if added, only **new** tombstones use the new policy. Existing tombstones keep their original `recoverable_until` (already encoded at delete-time).
- Eviction policy (D2) can override retention in steps 2–3 above; logged as `vault.eviction.tombstone_purged_early`.

---

## D6 — `remote_folders_cache` semantics

- Per-device decrypted snapshot of the **current** manifest's folder metadata.
- Refreshed on every manifest fetch; never edited locally except as a snapshot of the latest manifest.
- Local paths in `bindings` are independent — renaming a folder remotely does not rename local paths.
- Two devices binding the same remote folder to different local paths: both see remote rename on next refresh, neither local path changes.

---

## D7 — Android scope for v1

**Vault on Android is a separate post-v1 track.** v1 ships **desktop only**.

When Android work begins (post v1, separate plan addendum), it will be split into chunks roughly:

- A1: import + browse + manual download.
- A2: manual upload + soft delete + version download.
- A3: QR grant receive + revocation surface.
- A4 (v2): SAF-bound local folder + manual sync now.
- A5 (v2): WorkManager periodic sync + ransomware detection.

Phases T15 / T16 in file 11 are deferred to that addendum. Removed from the v1 critical path. The `tresor_v1`-equivalent Android capability bits do not need to be implemented in v1.

---

## D8 — Export passphrase ≠ recovery passphrase

- Export passphrase and recovery passphrase are **independent secrets**.
- Reuse is allowed but the export UX recommends a unique passphrase ("Use a passphrase you can share with the recipient — separate from your recovery passphrase").
- Argon2id parameters identical for both (file 03 cost target).

---

## D9 — Single vault per device, import = merge

- v1: each device has **at most one active vault per relay**. Schema is multi-vault-capable (`vault_id` PK everywhere) but UI/UX assumes a single vault.
- **Import into existing vault is a merge**, not a refusal. (Supersedes file 06 §"Import as new vault ID = v1 refuses".) Vault identity is verified first via genesis fingerprint; if fingerprints match the merge proceeds; if they differ the user gets a clear "this is a different vault — switch active vault, or cancel" prompt rather than a silent overwrite.
- During merge, when the import contains an entry that conflicts with the existing vault state, the user is presented with three options **per conflict batch** (not per file):
  - **Overwrite with imported** — incoming wins; existing version becomes restorable history.
  - **Skip conflicted imports** — existing wins; imported version added as restorable history.
  - **Rename imported conflicts** — incoming lands at `<original-path> (imported)` (and `<original-path> (imported N)` if collisions recur). Existing remains the canonical path.
- Conflict definition: **same logical path, different content fingerprint, both have a current (non-tombstoned) version**. Tombstones in either side never trigger the prompt; they are merged into history.
- Default selection: **Rename imported conflicts** (safest).
- Cross-ref: file 06 §"Merge import behavior" + §"Import conflict handling" — supersede with the above.

---

## D10 — Versioning vocabulary

- **Version**: an immutable entry inside `file.versions[]`. Has `version_id`, chunk list, content fingerprint, author device, timestamp.
- **Adding a new version of file F**: appends to `F.versions` and updates `F.latest_version_id`. The remote path is unchanged.
- **Renaming a file**: changes the path key under which the file lives. Does not create a new version.
- **Restoring a previous version**: appends a new version whose content is byte-identical to the chosen historical version (or, optimization, references the same chunks); updates `latest_version_id`. Old versions remain in history.
- File 07 §"Upload file in browser" "Add as new version" option uses this exact mechanic.

---

## D11 — Permission roles (canonical)

Four roles. File 03 §"Permission model", file 09 §"Permission recommendations", and file 10 §"Settings" use this exact list.

| Role | Browse | Download | Download versions | Upload | Soft delete | Sync | Hard purge | Grant other devices |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `read-only` | ✓ | ✓ | ✓ | — | — | — | — | — |
| `browse-upload` | ✓ | ✓ | ✓ | ✓ | ✓ | — | — | — |
| `sync` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | — |
| `admin` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

Eviction (D2 steps 2–3) requires `sync` or `admin`.

---

## D12 — Capability bits (granular)

Health endpoint `/api/health.capabilities` advertises a list. Each phase that adds an endpoint adds a bit. The relay-wide `vault_v1` bit is **only** advertised when **all of T1's mandatory bits** are present; clients gating on `vault_v1` get a complete v1 surface.

| Bit | Phase | Meaning |
|---|---|---|
| `vault_v1` | T1 | Aggregate: relay supports the v1 vault surface (implies the bits below). |
| `vault_create_v1` | T1 | `POST /api/vaults` works. |
| `vault_header_v1` | T1 | `GET/PUT /api/vaults/{id}/header` works. |
| `vault_manifest_cas_v1` | T1 | Manifest CAS PUT with `expected_current_revision`. |
| `vault_chunk_v1` | T1 | Chunk PUT/GET/HEAD + batch HEAD. |
| `vault_gc_v1` | T1 | `POST /api/vaults/{id}/gc/plan` works. |
| `vault_soft_delete_v1` | T7 | Server understands tombstone semantics for retention/GC. |
| `vault_export_v1` | T8 | Migration mode 2 (relay-to-relay) endpoints present (export bundle is purely client-side, but the server bit signals quota/headers handle large continuous transfers). |
| `vault_migration_v1` | T9 | `POST/GET/PUT /api/vaults/{id}/migration/*` endpoints (see H2). |
| `vault_grant_qr_v1` | T13 | `vault_join_requests` table + endpoints; QR-assisted grant flow supported. |
| `vault_purge_v1` | T14 | Delayed hard-purge job tracking on server. |

If a client requires a bit the relay doesn't advertise, the client refuses the operation with a clear error: *"This relay does not support `<feature>`. Update the relay or use a different one."*

Old transfer-only relays advertise none of the `vault_*` bits and continue to work for transfers/fasttrack.

---

## D13 — Storage isolation

- Vault chunks are stored at `server/storage/vaults/<vault_id>/<chunk_id_prefix>/<chunk_id>`.
- Vault chunks are **never** placed under `server/storage/transfers/`.
- Vault chunk IDs are prefixed `ch_v1_<random>` (random part is 24 base32 chars). Prefix lets every server log line + log search distinguish vault from transfer chunk activity.
- The legacy `DELETE /api/transfers/{id}` and transfer-cleanup paths cannot reach vault chunks.

---

## D14 — Operation-log segments

- Manifest field `operation_log_tail`: capped at **1000 entries**.
- When a write would exceed the cap, the client (atomic with the manifest CAS-update) **archives the oldest 500 entries into a new segment manifest**, leaving the newest 500 in `operation_log_tail`.
- Archived segments stored as separate relay rows, immutable once written. Schema: `vault_op_log_segments(vault_id, segment_id PK, seq, first_ts, last_ts, ciphertext, hash, created_at)`.
- Each segment is encrypted with HKDF subkey `dc-vault-v1/op-log-segment/<segment_id>` derived from the vault master key.
- Manifest header carries `archived_op_segments: [{seq, first_ts, last_ts, segment_id, hash}, ...]` — newest seq first. Manifest readers can fetch a specific segment on demand.
- **Segment rollover happens during a normal CAS publish**: it's just a manifest field change plus a new segment row, so it inherits CAS guarantees. If two clients race on rollover, the loser's segment row is garbage-collected (schema: `created_at` plus reference check from any current manifest).

---

## D15 — Preflight tombstone preview

In the connect-folder dialog, the preflight summary shows tombstones as a separate, informational line:

```
Remote folder "Documents":
  4.1 GB across 2,430 current files.
  380 deleted files (recoverable until 2026-06-01).
  Deleted files will not be applied to your local folder during initial binding.
```

Tombstones never produce local file deletions before the binding's initial baseline is captured. (Pairs with file 09 §"Sync binding as attack vector".)

---

## H2 — Migration state recovery

### State machine (per device, per vault)

```
idle → started → copying → verified → committed → idle (on new relay)
         ↑                                ↓
         └──────────  rollback ───────────┘ (only from started/copying/verified)
```

Persisted at `~/.config/desktop-connector/vault_migration.json` while non-idle.

```json
{
  "vault_id": "...",
  "state": "copying",
  "source_relay_url": "https://old.example.com",
  "target_relay_url": "https://new.example.com",
  "started_at": "2026-05-02T10:00:00Z",
  "verified_at": null,
  "committed_at": null,
  "previous_relay_url": null
}
```

### Server-side endpoints (T9, gated on `vault_migration_v1`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/vaults/{id}/migration/start` | Source relay records intent, returns migration token. Idempotent — calling again returns the existing token. |
| GET | `/api/vaults/{id}/migration/verify-source` | Source returns manifest hash, chunk count, ciphertext byte total. Client diff against target's `GET /api/vaults/{id}`. |
| PUT | `/api/vaults/{id}/migration/commit` | Source marks vault `migrated_to: <target_url>`. Vault becomes read-only on source. Idempotent. |
| GET | `/api/vaults/{id}/header` | After commit, returns header with `migrated_to` set. Other devices learn here. |

Chunk copy uses standard `PUT /api/vaults/{id}/chunks/{chunk_id}` on the target relay — no migration-specific upload endpoint.

### Recovery behaviors

| Crash point | Recovery |
|---|---|
| Mid `copying` | Resume: batch-HEAD on target, upload only the diff. |
| Post `verified`, pre `committed` | Next launch: prompt **[Switch] [Rollback] [Resume verify]**. Rollback deletes state file; target keeps the orphan vault (user can clean up via target relay UI later). |
| Post `committed`, app crash | Next launch: state file shows `committed`; client switches active relay URL, retains `previous_relay_url` for **7 days**, then drops it. |
| Network drop during verify | Resumable; verify is just diff over manifest hash + chunk count + byte total. |
| Target later vanishes within 7 days | "Switch back to previous relay" available in Settings. After 7 days, gone. |

### Multi-device propagation

- Migration is initiated by one device. Other devices keep using source relay until they next call `GET /api/vaults/{id}/header`.
- Source relay returns `migrated_to: <target_url>` in the header response after commit. Other devices switch automatically and store `previous_relay_url`.
- The migration-aware `GET /header` is itself the discovery mechanism. No new endpoint for migration notification.

### Out of scope for v1

- Multi-target migration (split a vault).
- Coordinated migration with explicit consent prompts on every device (relies on each device discovering on next sync — acceptable since vaults are read-only on source post-commit anyway).

---

## D16 — Vault-active toggle in main Desktop Connector settings

The main Desktop Connector Settings window gains a small **Vault** section, separate from the dedicated Vault settings window (which holds the deeper configuration). Two controls:

```
┌─ Vault ─────────────────────────────────┐
│  ☐ Vault active                          │
│     Show Vault in tray menu and sync.    │
│                                          │
│  [ Open Vault settings… ]                │
└──────────────────────────────────────────┘
```

### `Vault active` toggle

| State | Effect |
|---|---|
| **OFF** (default on fresh install with no vault) | Tray menu hides the entire Vault submenu. Sync engine stops cleanly: in-flight chunk uploads finish their current chunk + ACK, new ops blocked. Periodic background tasks halt (manifest refresh, GC eviction checks, ransomware detector). Notifications suppressed. **Local data preserved** (SQLite, downloaded chunks, history). Fully reversible. |
| **ON** (default after creating or importing a vault) | Tray menu shows the Vault submenu. Sync engine resumes from last persisted state. Pending operations from the OFF period are picked up where they were interrupted. |

The toggle is **never destructive** — it does not delete keys, manifests, downloaded chunks, or local index. Worst case "I changed my mind" recovery is a single click.

### `Open Vault settings…` button

- Launches the GTK Vault settings subprocess (handle as a new `--gtk-window=vault-settings` mode under the existing windows.py multiplexer).
- **Disabled** when the toggle is OFF (vault is dormant; opening the deep-config UI for a paused subsystem invites the user to fight the OFF state).
- **Hidden** when there is no vault configured at all (no vault to configure).

### Defaults and wizard routing

- **Fresh install: toggle is ON by default.** The Vault submenu shows in the tray immediately so the feature is discoverable.
- **Tray submenu visibility** = `toggle ON` (regardless of whether a vault exists yet).
- **Tray submenu contents**:
  - Toggle ON + no vault yet → entries are "Create vault…" and "Import vault…" — clicking either launches the create/import wizard.
  - Toggle ON + vault exists → full operating menu (Open Vault, Sync now, Export, Import, Settings).
- **`Open Vault settings…` button in main Desktop Connector Settings**:
  - Toggle OFF → greyed out.
  - Toggle ON + no vault yet → launches the create/import wizard.
  - Toggle ON + vault exists → launches the Vault settings window.
- **Wizard-cancellation rule** (covers a user who toggled ON but doesn't actually want a vault yet): if the user opens the create/import wizard via any of the above paths and **cancels without completing**, **and no vault exists yet**, toggle automatically flips back to OFF. They can re-enable any time by flipping the toggle ON again, which routes them to the wizard again.
- After a vault exists, toggle OFF / ON behavior is per the table above (hide submenu / stop sync, never destructive). The wizard-cancellation rule no longer applies.
- User-initiated toggle changes always survive across restarts.

### Use cases this serves

1. **Users who only want transfers / clipboard / find-device**: toggle stays OFF; Vault is invisible everywhere.
2. **Temporary pause**: maintenance window, low-bandwidth period, troubleshooting, "I'm about to mass-rename a folder, pause sync for an hour". Toggle OFF, do work, toggle ON.

### Out of scope

- Per-folder enable/disable (handled inside the Vault settings window, separate decision).
- Scheduled toggling (cron-style "off at night"). Manual only in v1.

### Cross-references

- File 10 §"Desktop entry points" — tray-menu visibility gates on this toggle.
- File 10 §"Desktop main Vault window → Settings" — link the **Open Vault settings…** path here; the in-vault Settings tab is the *deep* config; the main Desktop Connector Settings → Vault section is the *outer* gate.
- File 08 §"Sync directions" — the engine reads this toggle on every loop iteration and exits cleanly when off.
- File 09 — the toggle does **not** bypass eviction or destructive-action guards. It is a "this client doesn't participate right now" switch, nothing more.

---

## Closures (T0 open items, resolved 2026-05-02)

| gaps doc § | Topic | Lock |
|---|---|---|
| §1 | Recovery test prompt at vault creation | **Recommended, not mandatory.** Vault-create flow shows a "Test recovery now (recommended)" step with a clearly-labeled **Skip** button. Test status (`OK / Not tested / Failed / Stale`) is shown in Vault settings → Recovery; banner at top of Vault window if status is `Not tested` or `Failed`. |
| §6 | Ransomware detection thresholds | **Defaults**: 200 file changes within 5 minutes, OR ≥ 50 % rename ratio in a single sync batch. Both configurable in Vault settings → Sync safety. Detector itself can be disabled (with a confirmation dialog explaining the risk). Tighter than the gaps doc's 500/5min default — power users on bulk-edit workflows can loosen, defaults protect everyone else. |
| §13 | Vault unlock timeout | **Default**: 15 min idle + on-screen-lock + on-quit. Configurable in Vault settings → Security: `Never (until quit)` / `5 min` / `15 min` (default) / `30 min` / `1 hour` / `On every sensitive action` / `On screen lock only`. **Sensitive operations always require fresh unlock regardless of setting** (clear vault, hard purge, rotate Vault Access Secret, revoke admin device, change recovery material). |
| §16 | Export reminder cadence | **Default**: prompt monthly if no export has been made in the last 30 days. Configurable in Vault settings → Recovery: `Off` / `Weekly` / `Monthly` (default) / `Quarterly` / `Yearly`. Dismissable per occurrence; reminders never block workflow. |

---

## Vocabulary, defaults, and UX locks (gaps doc round-2 closures, 2026-05-02)

Locked here so plan files don't carry ambiguous vocabulary into later phases.

### gaps §2 — Emergency access status

Vault settings → Recovery shows a structured block at the top:

```
Emergency recovery
  Method:        Recovery kit + passphrase
  Last tested:   2026-04-15  (17 days ago)
  Status:        Ready

  [ Test recovery now ]   [ Update recovery material ]
```

`Status` values: `Ready` / `Untested` (never tested) / `Stale` (last test > 180 days ago) / `Failed` (last test failed) / `Missing` (no recovery configured — only possible in legacy/partial vaults; v1 vault creation requires recovery).

If `Status ∈ {Untested, Stale, Failed, Missing}`, a **persistent banner** appears at the top of the Vault window with a one-click "Test recovery now" action. Banner is dismissable for 7 days; reappears after.

### gaps §7 — Default file-exclusion list

Patterns excluded from sync by default (per remote folder, configurable):

```
# VCS metadata
.git/        .svn/        .hg/

# Build artefacts and dependency caches
node_modules/   vendor/      target/      build/      dist/
.gradle/        .idea/       .vscode/

# Language caches
__pycache__/    *.pyc        .mypy_cache/  .pytest_cache/

# OS metadata
.DS_Store       Thumbs.db    desktop.ini   .Trash-*/

# Editor / temp files
*.tmp           *.temp       *.swp         ~$*
```

Plus a **per-file size cap**: skip files > 2 GB by default, configurable per folder. Skipped files are logged as `vault.sync.file_skipped_too_large` so the user can tell.

Per-folder ignore list is stored in the **encrypted manifest** (so it's shared across paired devices, not relay-visible). User-supplied patterns use gitignore syntax. The default list above can be edited per folder (additions and removals).

### gaps §8 — Symlinks, FIFOs, hardlinks

| Object | Policy |
|---|---|
| Symbolic links | **Skipped by default**. Storing the symlink target as metadata is a v2 enhancement. |
| FIFOs / sockets / device files | **Always skipped**, never configurable. Logged as `vault.sync.special_file_skipped`. |
| Hardlinks (multiple paths to same inode) | Detected at scan time. Stored once in vault chunk-store; restored as **independent files** (not as hardlinks) on download — cross-platform restore reliability beats inode preservation. |

### gaps §9 — Case sensitivity

- The remote vault is **always case-sensitive**: encrypted manifest stores exact-case filenames.
- On case-insensitive local targets (HFS+ default, NTFS default, exFAT, FAT32), the preflight detects same-case-folded collisions and warns:

  > Two files in remote folder "Documents" differ only in letter case (`Report.pdf` and `report.pdf`). Your local filesystem cannot store both. Choose how to handle this.

- Resolution options:
  - `Keep one` — user picks which file to materialize; the other is staged at `.dc-vault-collisions/<original-path>.<sha8>` so nothing is hidden.
  - `Skip both` — neither is materialized; both stay accessible via Browser mode.
  - `Pick one for me` — last-modified wins; loser goes to `.dc-vault-collisions/`.
- Default: **never silently merge**.

### gaps §11 — Local disk-space preflight + temp file location

**Preflight points** (always run before the operation starts):

| Operation | Required = | Warning at | Block at |
|---|---|---|---|
| Download / restore | Decompressed size + 25 % margin | < 10 % free or required | < required |
| Export | Total ciphertext + 10 % margin | < 10 % free or required | < required |
| Import | Decompressed size + 25 % margin | < 10 % free or required | < required |

Insufficient space surfaces `vault_local_disk_full` (see Error codes) with a clear "free up X bytes on `<path>`" message.

**Temp-file location**: `~/.cache/desktop-connector/vault/temp/` by default. Configurable in Vault settings → Storage. Cleanup pass at app start removes any files older than 24 h. Atomic-rename pattern: write to `<target>.dc-temp-<uuid>`, fsync the file, fsync the directory, rename. Power-loss-safe.

### gaps §12 — Partial restore actions for v1

Exactly four actions, all targeting a path the user picks (not the bound folder unless explicitly selected):

| Action | Behavior |
|---|---|
| **Download file** | One file's current content → user-chosen path. |
| **Download folder** | Recursive download of a remote folder's current state → user-chosen path. No metadata layer (no manifest copy). |
| **Restore folder to date** | User picks a date; client finds the latest manifest revision ≤ date and materializes that folder's state at that point in time → user-chosen path. Existing files at the target are never overwritten — collisions are written as `<name>.restored-<timestamp>.<ext>`. |
| **Restore previous version of file** | One file, one version → user-chosen path. Does not change the vault's current state. |

**Out of scope for v1**: restoring directly into a bound folder (overwrite-with-care semantics is a v2 design). User must download to a side path and copy in manually if they want that effect today.

### gaps §14 — Device revocation UX wording

Locked verbatim. Used in the revoke-device confirmation dialog and in the post-revocation toast.

**Confirmation (before revoke):**
> "Revoking this device prevents it from making new changes to the vault. It does not erase data already downloaded by that device. Vault has no way to remove files from a device's local filesystem.
>
> If this device is lost or compromised, also rotate the Vault Access Secret to prevent any cached credentials from being reused."
>
> [ Revoke ]   [ Revoke and rotate access secret ]   [ Cancel ]

**Post-revocation banner (shown until dismissed):**
> "Device 'X' revoked. It cannot make new changes to the vault from now on. Files already downloaded to that device remain on its disk — Vault cannot reach them."

Vault Access Secret rotation lands in T13 (paired with H11). The "Revoke and rotate" combo button is preferred for compromised-device cases.

### gaps §15 — Relay backup warning

Educational text shown in two places, locked verbatim:

**(a) Vault settings → Recovery, beneath the emergency-access block:**
> "Your relay stores encrypted vault data, but the relay is not a backup service. Hardware failure, host migration, or operator mistake can lose this data. Vault is only fully safe if you keep a recent **export** somewhere outside the relay — a USB drive, another cloud, a different machine. Exports are encrypted with your export passphrase; only you can read them."

**(b) After a successful first export, in the export-completed dialog:**
> "Export saved. Keep at least one copy on storage that's independent of the relay (a different device, another cloud, removable media). If the relay is ever lost, the export is your only path back to your vault."

### gaps §17 — Import preview detail

Fields shown in the import-preview dialog, in this order:

1. **Vault fingerprint** — full hash with first-12-chars highlighted; sub-line: "matches active vault" / "different vault" / "no active vault".
2. **Source** — relay URL if relay-to-relay; "File: `<filename>`" if file-based.
3. **Vault size** — `<logical> / <ciphertext>` (e.g., `4.2 GB / 4.5 GB ciphertext`).
4. **Remote folders** — count + per-folder list (name, file count, logical size). Truncate to top 10 with "... + N more".
5. **History** — `X current files / Y versions / Z tombstones`.
6. **Conflicts with active vault** — `N` (only shown if importing into existing vault per D9).
7. **Head impact** — `Will change current head: yes / no` (yes if any current-file overrides; no if pure history merge).
8. **Bandwidth preview** — `X of Y chunks already on this relay (will skip)` so the user knows roughly how much data must transfer.

Dialog footer: `[ Import ]   [ Cancel ]`. If conflicts > 0, primary button is `[ Resolve conflicts → ]` instead, leading into D9's three-way picker.

### gaps §19 — Integrity check default

Two levels:

| Level | What it does | Speed | Default |
|---|---|---|---|
| **Quick** | Verify manifest hash chain (genesis → current); verify chunk-index entries reference real chunks of expected size; AEAD-verify the current manifest only. | Seconds for typical vaults. | **On-demand default**. Also runs automatically once a week if the app is running and idle for 30+ minutes. |
| **Full** | Decrypt and AEAD-verify every manifest revision and every chunk. | Minutes-to-hours depending on vault size. | **Manual only.** |

UI: Vault settings → Maintenance → "Verify integrity" with two buttons. Result panel lists any failed items.

**Repair**:
- Quick fail → prompts to run Full check.
- Full fail → lists affected items; user can choose to mark them as broken in a new manifest revision (purges them from the live tree but keeps them in op-log for audit) or restore those specific items from a known-good export. **Never auto-repairs by deletion.**

### gaps §20 — Sync mode vocabulary

Five values, locked. Used in binding state, UI labels, and config. (Replaces the four states in plan file 02 §"Binding states" — the names there get realigned to these.)

| Mode | Direction | Effect on local | Effect on remote |
|---|---|---|---|
| `Browse only` | — | None (no binding). | None. |
| **`Backup only`** | Local → Remote | Watcher runs; uploads new/changed local files. | Manifest grows; remote changes never pulled down. |
| `Two-way sync` | Both | Remote changes applied to local; local changes pushed up. | Mirrored. |
| `Download only` | Remote → Local | Remote changes applied; local changes ignored / not uploaded. | Untouched. |
| `Paused` | — | Binding exists, no traffic. | No traffic. |

**Default for new bindings: `Backup only`** (per gaps doc rec — the safest mode for a typical user "I have files, I want them safe"). User can switch the mode any time without disconnecting.

### gaps §21 — Activity timeline storage (two-layer)

| Layer | Storage | Visibility | Default |
|---|---|---|---|
| **Vault op-log** (already in §D14) | Encrypted, in manifest's `operation_log_tail` + archived segments. Stored on relay. | Shared across all paired devices. | **Always on.** Captures: file create/update/delete, folder rename, version restore, device grant create/revoke, eviction events, mode changes. |
| **Local per-device log** | `~/.config/desktop-connector/logs/vault.log` — plaintext rotating file. | Local-only. | **Off by default.** Gated on the existing "Allow logging" toggle in main settings (which already controls non-vault logging). When on, captures: API calls (URLs only, no payloads), AEAD failures, sync stalls, file-stability waits, integrity check results. |

**UI**: Vault settings → Activity is the human-readable timeline view of the vault op-log. The local log is downloadable via main settings → Logs → Download (existing flow extended to include `vault.log`).

**Never logged in either layer**: keys, passphrases, decrypted file content, decrypted filenames.

### gaps §22 — Local-effects vocabulary (canonical four)

Locked verbatim. Used in confirmation dialogs, settings descriptions, and any user-facing text that mentions one of these actions. Plan files 02, 07, 09, 10 must use these wordings exactly.

| Action | Effect |
|---|---|
| **Disconnect** (folder) | Stops sync. Local files stay where they are. Remote data unchanged. Reversible — reconnect any time and choose a sync mode. |
| **Delete** (file or folder) | Soft-deletes from the vault (creates a tombstone). Files on devices that already downloaded them stay on those devices. Recoverable until the retention window passes. |
| **Clear** (folder or whole vault) | Soft-deletes everything inside. Same retention rules as Delete. Files on already-synced devices stay there. Local files in bound folders are **not** removed. |
| **Purge** (admin only) | Permanently destroys deleted-file chunks on the relay after a 24-hour delay. Cannot be undone. Files on devices that downloaded them stay on those devices — Vault has no way to reach across to your filesystems. |

> **Bedrock**: All four operations only affect data on the **relay** and the **local index**. None of them remove files from already-synced local folders on other devices. The vault has no remote-delete capability against your own filesystems.

---

## D1 — Manifest format versioning (reservation only in T0)

- Manifest header reserves a `manifest_format_version` field. v1 sets it to `1`.
- Future folder-manifest split, op-log compaction strategy changes, or new field additions bump this. Old clients reading a higher version refuse to mutate (read-only fallback) and prompt for app update.
- No semantic split of manifests in v1.

---

## Out of scope for v1 (explicit)

These are **not** in v1 scope. Surfaced here so they're not silently assumed.

- Vault Master Key rotation. (Mitigation if compromised: create a new vault, migrate. Vault Access Secret rotation **is** in v1 — see file 03.)
- Per-folder retention policy changes after creation.
- Automatic Android folder sync (D7).
- Multi-vault per device.
- "Import as new vault ID" (a different operation than D9's merge — it would create a copy under a fresh identity; deferred to v2).
- Folder-level export/import bundles. Only full-vault export in v1.
- Encrypted activity log shared across devices vs local-only — gaps doc §21 deferred to T17 design.

---

## Error codes (vault_v1)

All vault-related API errors and client-local error states use stable string codes from the table below. The wire format is:

```json
{
  "ok": false,
  "error": {
    "code": "vault_manifest_conflict",
    "message": "The vault manifest changed on the server.",
    "details": { "current_revision": 43, "expected_revision": 42 }
  }
}
```

- `code` is mandatory and stable forever (additions are fine; renames and meaning-changes are not).
- `message` is human-readable English; clients may localize.
- `details` is per-code (see "Required `details`" column below); fields not listed are reserved for future additions and clients must ignore unknown fields.
- Codes not in this table are treated by clients as `vault_internal_error` with the unknown code preserved for logs.

**Retry classes** drive client behavior:

- **auto** — client retries automatically with exponential backoff (capped per the existing transfer retry budget logic).
- **user-action** — client surfaces the error to the user; retry only happens when the user explicitly resolves it (e.g. frees space, re-enters passphrase, switches relay).
- **permanent** — do not retry; user is shown a terminal error.
- **info** — not actually an error in flow terms; the client transitions to a handled state (e.g. enter merge UX). Surfaced in logs as info, not error.

Existing relay-wide errors (`payload_too_large`, generic 4xx with no `code`) are unchanged. **Vault endpoints always emit a `code`** when they error.

### Auth & access

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_auth_failed` | 401 | permanent | `kind: "device"\|"vault"` | Auth header(s) missing or invalid. `kind` distinguishes device-pair auth from vault-access auth. |
| `vault_access_denied` | 403 | permanent | `required_role` | Caller's role insufficient for the operation. |
| `vault_not_found` | 404 | permanent | `vault_id` | Vault does not exist on this relay. |
| `vault_already_exists` | 409 | permanent | `vault_id` | `POST /api/vaults` collided with an existing `vault_id`. |

### Manifest & integrity

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_manifest_conflict` | 409 | auto | `current_revision`, `expected_revision` | CAS mismatch. Client runs the D4 merge algorithm and retries. |
| `vault_manifest_tampered` | 422 | permanent | `revision`, `expected_hash`, `actual_hash` | Manifest hash chain or AEAD verification failed on read. |
| `vault_header_tampered` | 422 | permanent | `expected_hash`, `actual_hash` | Vault header AEAD verification failed. |
| `vault_format_version_unsupported` | 422 | permanent | `seen_version`, `max_supported_version` | `manifest_format_version` is newer than this client understands (D1). Client falls back to read-only and prompts for app update. |

### Chunks

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_chunk_missing` | 404 | auto | `chunk_id` | Chunk referenced by the manifest is not on the relay. Could be transient during another writer's upload window; retry budget then surface as permanent. |
| `vault_chunk_tampered` | 422 | permanent | `chunk_id`, `expected_hash`, `actual_hash` | Chunk AEAD or hash verification failed. |
| `vault_chunk_size_mismatch` | 422 | permanent | `chunk_id`, `expected_size`, `actual_size` | Stored ciphertext size differs from manifest's declared size. |

### Quota & storage

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_quota_exceeded` | 507 | user-action | `used_bytes`, `quota_bytes`, `eviction_available: bool` | Write would exceed quota (D2). If `eviction_available=true`, client offers eviction; if false (or after eviction exhausted), surfaces "vault full, sync stopped" banner. |
| `vault_local_disk_full` | — | user-action | `required_bytes`, `available_bytes`, `path` | Client-local: not enough free space on the target volume (download, restore, export, import). |
| `vault_storage_unavailable` | 503 | auto | — | Relay-side I/O issue (disk error, FS unmounted, etc.). |

### Import / export / migration

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_export_tampered` | — | permanent | `section`, `reason` | Outer envelope, header, manifest record, or chunk record failed verification on import. `section` is one of `envelope` / `header` / `manifest` / `chunk` / `index` / `footer`. |
| `vault_export_passphrase_invalid` | — | user-action | — | Outer-envelope decryption failed. Client prompts user to re-enter export passphrase. |
| `vault_identity_mismatch` | 409 | permanent | `seen_fingerprint`, `expected_fingerprint` | Vault genesis fingerprint differs from the active vault. Surfaces "this is a different vault" prompt; never silent overwrite (D9). |
| `vault_import_requires_merge` | — | info | `conflict_count`, `current_count`, `imported_count` | Import hit per-path conflicts (D9). Client transitions to the three-way conflict UX (Overwrite / Skip / Rename). Not really an error — info-class. |
| `vault_import_failed` | — | permanent | `reason` | Catch-all for malformed import bundles where no more specific code applies. `reason` is free-form. |
| `vault_migration_in_progress` | 409 | user-action | `state`, `target_relay_url` | Server has an in-flight migration; the requested op is incompatible with the current state (e.g. trying to commit before verify). State machine values per H2. |
| `vault_migration_verify_failed` | — | user-action | `mismatch: ["manifest_hash"\|"chunk_count"\|"byte_total"\|"chunk_sample"]` | Source/target diverged during verify. User decides to re-copy, abort, or rollback. |

### Recovery

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_recovery_failed` | — | user-action | — | Recovery passphrase / kit did not produce a valid Vault Master Key. Client prompts re-entry. Never reveals which part (passphrase vs kit) was wrong. |
| `vault_recovery_not_configured` | — | permanent | — | Vault has no recovery envelope set (only possible in legacy / partial-state vaults; v1 vault creation requires recovery setup). |

### Permission (specific)

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_purge_not_allowed` | 403 | permanent | `required_role: "admin"` | Hard-purge attempted by a non-admin. Distinct from the general `vault_access_denied` so UI can surface the specific upgrade path ("ask an admin device to purge"). |

### Capability & protocol

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_protocol_unsupported` | 426 | permanent | `required_capability` | Relay does not advertise a capability bit the client requires (D12). |
| `vault_client_too_old` | 426 | permanent | `min_required_client_version` | Server explicitly rejected the client version (used when a security-relevant fix forces a hard floor). |
| `vault_server_too_old` | — | permanent | `required_capability` | Client-side: relay's `vault_v1` aggregate bit is missing or below the client's required level. |

### Sync (client-local only)

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_sync_paused_suspicious_change` | — | user-action | `change_count`, `window_seconds`, `rename_ratio`, `threshold_changes`, `threshold_window_seconds`, `threshold_rename_ratio` | Ransomware detector tripped (Closures gaps §6 thresholds). User decides to review, rollback, resume, or keep paused. |
| `vault_sync_paused_quota_drained` | — | user-action | `quota_bytes` | Eviction exhausted; sync stopped (D2 step 4). User must free space or migrate. |
| `vault_unlock_required` | — | user-action | `reason` | Sensitive op requires fresh unlock per the lock policy (Closures gaps §13). `reason` is `idle_timeout` / `screen_lock` / `quit` / `sensitive_action`. |

### Rate limit

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_rate_limited` | 429 | auto | `retry_after_ms` | Per-vault or per-device rate limit hit. Client respects `Retry-After` header and `retry_after_ms` (the latter is more precise). |

### Generic

| Code | HTTP | Retry | Required `details` | Meaning |
|---|:---:|:---:|---|---|
| `vault_invalid_request` | 400 | permanent | `field`, `reason` | Request payload malformed at the API contract level. `field` names the offending parameter when applicable. |
| `vault_internal_error` | 500 | auto | — | Catch-all relay-side error. Logged with full detail server-side; client retries with backoff and surfaces if persistent. |

### v2+ codes (reserved, not emitted in v1)

These names are reserved by T0 so their adoption later doesn't collide with anything else: `vault_key_rotation_in_progress`, `vault_grant_expired`, `vault_grant_revoked`, `vault_folder_locked`, `vault_offline_pending`.

---

## Implementation clarifications (audit closures, 2026-05-02)

A pre-T1 audit surfaced 21 places where the existing T0 lock + plan files left a real choice unspecified. Closures below; section IDs match audit findings (A1–A21) so future references stay stable.

### Server / API contracts

**A1 — Manifest CAS 409 response includes the current manifest ciphertext.**
On `vault_manifest_conflict`, the server returns `details: { current_revision, current_manifest_hash, current_manifest_ciphertext, current_manifest_size }`. The client never has to issue a follow-up `GET /manifest` after a 409 — it has everything it needs to run the §D4 merge. Keeps the retry loop one round-trip and removes the race between 409 and a separate GET landing on a yet-newer revision.

**A5 — Vault Access Secret rotation is single-active-hash + transactional re-auth.**
Server stores **one** active `vault_access_token_hash`. The rotation endpoint takes both old and new tokens; server validates old, then atomically replaces the hash. No multi-hash grace window on the server. Client side: in-flight requests holding the old bearer finish; new requests use the new bearer. The "7-day grace" is **client-side**: the user is given 7 days to grant other devices the new secret (over QR / shared kit) before old caches stop working in practice. Defends against compromised-device replay.

**A8 — Tombstone `deleted_at` is informational only.** Confirms §D5: client-supplied `deleted_at` is stored as-is for display, never validated against server time, and **never used** to compute `recoverable_until`. Server uses its own clock at GC planning time. A client with a 6-month-skewed clock cannot accelerate or delay purge.

**A19 — Chunk ID format is strictly validated.** Pattern: `^ch_v1_[a-z2-7]{24}$` (`ch_v1_` literal + 24-char base32 alphabet). Server rejects any deviation with `vault_invalid_request` + `details.field = "chunk_id"`. Future versions can use `ch_v2_...`; the strict prefix gate prevents v1 servers from accidentally storing v2 blobs.

### Crypto and formats

**A3 — `manifest_format_version` is a plaintext field in the manifest envelope, *not* AAD.** Old clients reading a higher version number must reject with `vault_format_version_unsupported` **before** attempting decryption. Manifest AAD remains: `dc-vault-manifest-v1 || vault_id || manifest_revision || parent_revision || author_device_id` (concatenation, fixed-length encoding, see vault-v1 protocol doc to be drafted in T0.2).

**A7 — Concurrent-version tie-breaker timestamp is client-provided.** When two devices add a version to the same file, ordering for `latest_version_id` is `(timestamp, device_id_hash)` where `timestamp` is the client's wall-clock at upload time, encoded as RFC 3339 ms-precision in the version metadata. Server does not normalize. Tie on equal timestamp is broken by `device_id_hash` (SHA-256 of device id, big-endian lex compare). Two clients merging the same conflict converge.

**A10 — Export bundle is binary streamable, CBOR-framed.**
Physical layout, in order: (1) Outer envelope header (magic bytes `DCVE` + envelope format version + Argon2 params + nonce); (2) AEAD-streamed body containing CBOR records: `[record_type, length, payload]` where `record_type ∈ { export_header, bundle_index, manifest, op_log_segment, chunk, footer }`; (3) Footer record with overall hash + record count. Reader processes record-by-record without buffering the whole file. CBOR chosen over MessagePack for canonical-encoding RFC8949 spec → bit-identical bundles for identical input.

**A18 — Test vectors live at `tests/protocol/vault-v1/` as JSON arrays.**
File-per-primitive: `manifest_v1.json`, `chunk_v1.json`, `header_v1.json`, `recovery_envelope_v1.json`, `export_bundle_v1.json`, `device_grant_v1.json`. Each file is a JSON array of cases. Each case: `{ name, description, inputs: { …hex/base64-encoded keys, plaintexts, AADs… }, expected: { ciphertext, hash, …or expected_error: "vault_…" }, notes? }`. Mirrors the existing `tests/protocol/test_*` pattern (Python harness reads JSON, exercises both desktop Python crypto and server PHP crypto).

### Roles, naming, UX vocabulary

**A4 — Conflict batch granularity = one remote folder per dialog.**
On import-merge (§D9), the user is prompted folder-by-folder. Each prompt covers all path-conflicts within that single remote folder. The chosen mode (Overwrite / Skip / Rename) applies to every conflict in that batch; the user moves to the next folder's batch on confirm. "Apply to all remaining folders" is offered as a checkbox after the first prompt.

**A9 — Role names: hyphen-lowercase, canonical.**
Everywhere: `read-only`, `browse-upload`, `sync`, `admin`. Plan file 03 example JSON (which currently shows `read_only` with underscore) is wrong — fix in §D11 enforcement during T3 implementation. Wire format, storage, UI labels, test vectors all use hyphen-lowercase.

**A11 — Recovery kit is a file; the QR is an optional rendering of that file.**
Default artifact: `<vault-id>.dc-vault-recovery` — a file containing plaintext metadata (vault id, creation date, instructions in user's language) plus base32-encoded recovery secret + Argon2 params. The desktop app can render the base32 portion as a QR code for transferring to another device, but the **file is the primary artifact**. The 24-word mnemonic option is deferred to v1.5 (BIP-39 list, optional secondary representation of the same secret).

**A12 — Binding state and sync mode are independent axes.**
- **Binding state** (where the local connection is in its lifecycle): `unbound` / `needs-preflight` / `bound` / `paused` / `error`. `unbound` means no local path is connected (browser-mode access still works).
- **Sync mode** (data direction when bound): `Backup only` (default) / `Two-way` / `Download only`.
- A binding always has both a state and a mode. `Paused` binding state preserves the mode for resume. `Browse only` from §gaps §20 maps to `binding_state = unbound`; it's not a sync mode.
- File 02 §"Binding states" (six states, including `browse_only`) is superseded by this five-state taxonomy.

**A20 — Conflict-file naming is one shared scheme.**
`<original-name> (conflict <kind> <device-or-tag> <YYYY-MM-DD HH-MM>).<ext>`. Examples:
- Local sync conflict: `report (conflict from Laptop 2026-05-02 17-30).docx`
- Browser upload conflict: `report (conflict uploaded Laptop 2026-05-02 17-30).docx`
- Import merge conflict: `report (conflict imported 2026-05-02 17-30).docx`
- Recursion: `report (conflict imported 2026-05-02 17-30) (conflict imported 2026-05-02 18-00).docx`

One utility, three call sites.

### State machines and lifecycle

**A2 — Vault-active toggle: discoverable-by-default + wizard-routing-on-cancel.**
Fresh install: toggle is **ON**. Tray submenu always reflects toggle state, but its contents pivot on whether a vault exists (no-vault → "Create / Import…" launchers; vault-exists → full operating menu). Cancelling the create/import wizard while no vault exists flips the toggle back to **OFF** so the user isn't permanently nagged. Re-enabling the toggle re-launches the wizard. See updated §D16 defaults for the full state table.

**A6 — Eviction is per-device, triggered by the device that hits 507.**
When a device's write returns `vault_quota_exceeded` with `eviction_available=true`, that device runs the §D2 eviction pass and retries. Other devices learn about the freed space on their next manifest fetch. No central authority, no inter-device coordination — the manifest CAS is the consistency mechanism, and eviction is just a sequence of soft-deletes-on-old-versions which CAS-merges normally. A second device hitting 507 simultaneously will run its own pass; if both delete the same old-version chunk, the CAS layer dedups (one wins, the other gets `vault_chunk_missing` next fetch and re-plans).

**A13 — Op-log tail cap is global per vault, not per device.**
`operation_log_tail` carries the latest 1000 ops from **any** device. When a write would exceed 1000, the writer archives the oldest 500 (regardless of which device produced them) into a new segment manifest in the same CAS update. Segments are visible to all devices via the manifest's `archived_op_segments` list — the encrypted op-log is a single shared stream, not a per-device journal.

**A14 — T13 v1 deliverable: rotate Vault Access Secret only.**
Recovery passphrase rotation (re-wrap the recovery envelope with a new user passphrase) is **v1.5**. Vault Master Key rotation is **v2** (or solved by migrate-to-new-vault per §H11). UI in T13: Vault settings → Security shows only the access-secret rotation control. Recovery section has a placeholder text "Change recovery passphrase — coming in v1.5" rather than a non-functional button.

**A15 — Ransomware detector pauses immediately, no pre-pause prompt.**
When thresholds trip (200 changes / 5 min OR ≥50% rename ratio), the watcher transitions the binding to `paused` synchronously, then surfaces a banner: *"Suspicious activity detected — sync paused. [Review changes] [Rollback to previous version] [Resume sync] [Keep paused]"*. No "Continue anyway?" pre-pause prompt. The default reaction must be safe; the user can resume after looking. After 7 days of `paused` + ignored, a second banner warns about data divergence if they disconnect without resolving.

**A16 — GC has three triggers, all initiated by clients.**
- **Sync-driven** (automatic): on every manifest fetch, the client checks whether any tombstones in the manifest have `recoverable_until < now`. If so, it builds a candidate-chunk list and calls `POST /api/vaults/{id}/gc/plan` opportunistically. Server returns the safe-to-delete subset, client confirms via `POST /api/vaults/{id}/gc/execute`. This is fire-and-forget housekeeping; no user-visible UX unless it fails.
- **Eviction-driven**: §D2 step 1 calls the same GC endpoint with expired-tombstone candidates first.
- **Manual**: Vault settings → Maintenance → "Optimize storage now" runs the full plan immediately.
- Scheduled background GC (e.g. weekly cron) is v1.5+.

**A17 — Vault-active toggle OFF cancels pending hard-purges and clears pending-purge state.**
Pending hard-purge schedules (T14) live in a local state file `~/.config/desktop-connector/vault_pending_purges.json`. Toggling Vault OFF clears this file *and* tells the server to cancel the scheduled purge job (`POST /api/vaults/{id}/gc/cancel?job_id=...`). Server cancels are idempotent. Toggling back ON does **not** restore cancelled purges — user must re-schedule. In-flight uploads/downloads finish their current chunk + ack, then exit cleanly. Pending sync ops (un-uploaded local changes) are preserved and resume on toggle ON.

### Per-folder accounting

**A21 — Quota counts unique chunks across the whole vault; per-folder displays count chunks-referenced-by-that-folder.**
Server enforcement is on the global ciphertext byte total: each chunk counts exactly once toward `vaults.used_ciphertext_bytes`. Per-folder usage display is **descriptive** (chunks referenced by current entries in that folder), not enforcement. Two folders sharing the same chunk both display its size; their per-folder displays sum to more than the whole-vault total. UI labels: per-folder = *"Stored data in this folder"*; whole-vault header = *"Total remote storage used"*. No cross-folder dedup logic is needed in v1.

---

## Out-of-scope-for-v1 (audit additions)

- BIP-39 24-word recovery mnemonic (alternative recovery format).
- Folder-level export bundles (`.dc-vault-folder-export`).
- Recovery passphrase rotation (re-wrap envelope).
- Vault Master Key rotation.
- Scheduled background GC.
- Auto-merge "import as new vault id" (creates a copy under fresh identity — different from §D9 merge).
- Restoring previous version directly into a bound folder (must download to side path in v1).
