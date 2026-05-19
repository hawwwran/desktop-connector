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

### 2026-05-19 — Vault config knobs: `vaultQuotaBytes` + floor-protected `vaultAuthLimit`

**Status:** landed.

**Context.** B5 live test (2026-05-19,
``temp/automation-tests-results/0005/B5-eviction/result.md``) surfaced
two server-side config drifts. (1) ``vaultQuotaBytes`` was referenced
by ``CLAUDE.md`` and ``docs/plans/skipped-while-autonomous.md`` as
configurable but no controller read it; ``VaultsRepository::create``
omitted the column from its INSERT, so every freshly-minted vault
inherited the schema default of 1 GB regardless of operator intent.
(2) ``VaultAuthService::AUTH_LIMIT`` was deliberately hardcoded at 10
attempts/(device, vault)/minute to throttle compromised paired
devices; the live test showed legitimate sync workloads bill one
auth attempt per chunk PUT and trip the cap inside a single drain
cycle (5 chunks + batch-head + root fetch + shard publish > 10).

**Decision.** Wire ``vaultQuotaBytes`` through
``VaultsRepository::create`` so the create endpoint stamps the
configured cap onto each new vault row; existing rows aren't
retroactively resized. Add ``vaultAuthLimit`` as a runtime-readable
cap with a *floor* of 10: ``Config::vaultAuthLimit()`` returns
``max(configured, VAULT_AUTH_LIMIT_FLOOR)`` so an operator can raise
the cap on a dedicated host but a config typo can never lower it
below the original §1.H1 design. Window sizes + ``CREATE_LIMIT``
stay hardcoded.

**Alternatives.** (a) Make ``AUTH_LIMIT`` freely configurable
(rejected — would let a typo weaken the §1.H1 throttle protecting
against a compromised paired device, the original threat model). (b)
Change the billing semantics so only failed bearer-match attempts
count (rejected here — larger behaviour change, would need its own
ADR + careful audit; the floor-protected config approach gives
operators an escape hatch immediately without re-litigating the
billing rule). (c) Wire ``vaultQuotaBytes`` as a global cap applied
to existing rows on first-read (rejected — destructive surprise for
operators who deliberately raised individual vaults; the new key
deliberately affects only freshly-created vaults).

**Anchor.** ``server/src/Config.php`` (``vaultQuotaBytes()`` +
``vaultAuthLimit()`` + ``VAULT_AUTH_LIMIT_FLOOR``);
``server/src/Repositories/VaultsRepository.php::create`` (optional
``$quotaCiphertextBytes`` parameter, conditional INSERT shape);
``server/src/Controllers/VaultController.php::create`` (call-site
passes ``Config::vaultQuotaBytes()``);
``server/src/Auth/VaultAuthService.php`` (the comparison reads
``Config::vaultAuthLimit()`` per attempt). Tests:
``VaultControllerTest::test_create_stamps_configured_vault_quota_bytes``,
``VaultAuthServiceTest::test_configured_vault_auth_limit_raises_cap_above_floor``,
``VaultAuthServiceTest::test_vault_auth_limit_below_floor_clamped_to_ten``.

### 2026-05-18 — Rotation crash-recovery via plaintext-secret marker file

**Status:** landed (commit ``f352dbe``).

**Context.** The §5.H3 access-secret rotation wizard has a narrow but real bricking window: between ``POST /api/vaults/{id}/access-secret/rotate`` returning 200 (relay now holds the new secret) and ``store.save(new_grant)`` succeeding (local keyring updated), a SIGKILL / OOM / GTK crash leaves the relay accepting only the new secret while the keyring still holds the old. For a single-device user with no peer device available to re-grant via §5.C2, every subsequent vault op returns 401 — the vault is unrecoverable from that device. The pre-rotation kit carries the OLD ``vault_access_secret`` so it can't unbrick either.

**Decision.** Persist a marker file ``<config_dir>/vault_rotation_in_progress_<vault_id>.json`` containing the new secret in plaintext, mode 0600, written via ``atomic_write_file`` BEFORE the relay POST. Cleared after ``store.save`` returns. On wizard relaunch a dedicated recovery page probes the relay with the cached secret:

- HTTP 200 → relay still accepts the old secret → rotation never committed → discard marker.
- HTTP 401 → relay has the new secret → save marker's ``new_secret`` into the keyring → discard marker.
- Network error → preserve marker; offer Retry button.

**Threat model.** The marker introduces a new at-rest secret class on disk. Mitigation: ``mode 0o600`` matches the recovery-kit file's mode, anchoring on the same threat model — physical custody by the user, no protection against a co-resident process running as the same uid. The keyring backend (libsecret) already protects the same secret post-recovery; the marker just extends the exposure window from "process lifetime" to "until next wizard launch." A passive disk-image attacker (backup, snapshot) gets the secret iff the snapshot happened during the bricking window, which is bounded by the user noticing the issue + re-running the wizard. Acceptable for v1.

**What we deliberately did NOT do.** No passphrase-encrypted marker — the recovery path needs to read the marker on a device that has no fresh passphrase to derive a wrap key against (it just crashed mid-rotation; the previously-verified passphrase is gone from memory). Asking the user for the passphrase before the recovery probe doubles the UX burden for a recovery step that already requires the user's attention. No atomic-swap protocol on the relay side — the server doesn't know which device's rotation is in flight, and a "swap two secrets" handshake would expand the API surface beyond what one rare failure mode justifies.

**Anchor.** ``desktop/src/vault/grant/rotation_recovery.py`` owns the marker shape + probe. Wizard integration: ``desktop/src/windows_vault_rotate.py`` (recovery page + worker). Diagnostics: ``vault.rotate.marker_*`` and ``vault.rotate.probe_*`` event family in ``docs/diagnostics.events.md``.

### 2026-05-18 — Multi-device device-add path: QR-grant primary, recovery kit secondary

**Status:** landed.

**Context.** Before the 2026-05-18 design pass the multi-device story for vault recovery was kit-shuffle only: each new device needed the recovery kit file + the passphrase. QR-grant primitives (``vault/grant/qr.py``, ``vault/grant/wrap.py``, server ``/join-requests`` + ``/device-grants``) shipped end-to-end but had zero UI callers; capability ``vault_grant_qr_v1`` was advertised but unreachable. A memory note classified the UI build as a v1.x deferral.

**Decision.** Reverse the deferral. v1 ships QR-grant as the primary "add another device" path (§5.C2 admin dialog + claimant subprocess); the recovery kit stays as the secondary recovery surface for "lost every device, restore from kit" scenarios. The reversal is anchored by the simultaneous §6.H2 (Devices tab + revoke) build: a vault that can grant device access must also be able to revoke it, and a vault that can revoke must offer a grant path the operator actually uses. Shipping v1 with §6.H2 (revoke) but no §5.C2 (grant) would leave the protocol primitives sitting unused while users rely on the kit shuffle — coherent only with both shipping together.

**What we deliberately did NOT do.** No webcam QR scanning in v1 — the ``vault://`` URL paste path covers the same UX without the Wayland portal + GStreamer dependency. Deferred to v1.x. No mid-session role downgrade UI — the admin picks the role at approval time and revocation is the only post-approval lever. No multi-claimant batching — F-S13's CAS-on-pending caps one approved claim per join-request.

**Anchor.** Admin in-process dialog ``desktop/src/windows_vault/grant_device_dialog.py`` + Devices tab "Grant a new device…" button; claimant subprocess ``desktop/src/windows_vault_join.py`` + tray submenu's "Add this device to a vault…" entry (visible when no local vault exists). Typed client ``desktop/src/vault/grant/join_client.py``. Memory note ``project_vault_multi_device_story.md`` records the user-facing rule "QR-grant is the primary multi-device path; kit is the fallback for total-loss recovery."

### 2026-05-18 — Shard envelope author check relaxed at expected=0; root path stays strict

**Status:** landed (commit ``b048d86``).

**Context.** During the 2026-05-18 review pass on §5.M2 the migration runner appeared to fail because the server rejected ``new_shard_revision != expected + 1``. Actually testing it showed the chain check (``parent == new - 1``) is orthogonal to the CAS check (``expected == current``); genesis-insert at arbitrary revision already worked on the server. The genuine failure mode was ``validateShardEnvelopeAgainstBody``'s unconditional ``env.author_device_id == X-Device-ID`` check: migration replicates a source-side shard envelope verbatim, and that envelope's author is whichever peer wrote it on the source, not the migrating device. Result: every multi-device-vault migration died on the first peer-authored shard.

**Decision.** ``validateShardEnvelopeAgainstBody`` gains an ``$allowEnvelopeAuthorMismatch`` flag. Both ``putShard`` and ``putShardWithRoot`` pass ``true`` when ``expected_current_shard_revision === 0`` — the genesis-insert path is also the migration-replication path. Non-genesis edits still enforce strict author match (regression-pinned by ``test_putShard_rejects_foreign_envelope_author_on_edit``).

**Author field is metadata, not a security boundary.** All paired devices share ``master_key`` and can already construct any envelope they want with any author claim. AEAD AAD binds the author cryptographically (decrypt with the wrong author fails). The pre-fix check rejected a forged author at WRITE time, but a malicious paired device could just put its OWN author on a forged-content envelope and the check passes anyway. The check provides no isolation between paired devices; the genuine access control is the vault-bearer + X-Device-ID + per-role check.

**Asymmetry vs ``putRoot``.** The corresponding root-only path (``putRoot``) still enforces strict ``env.author_device_id == X-Device-ID``. Intentional: migration replicates root envelopes via the ``create_vault`` path (initial_root_ciphertext on first vault create), not via ``putRoot``. ``create_vault`` doesn't check author at all (no row exists yet). After ``create_vault``, the target vault's root only mutates via ``putRoot``/``putShardWithRoot`` and those mutations come from the migrating device (root advances during migration's verify-then-commit phase), so the strict check is correct there. If a future migration path ever needs to replicate a peer-authored root post-create, this asymmetry would have to be revisited; ``test_putShardWithRoot_accepts_foreign_envelope_author_on_genesis`` pins the shard side of the same case.

**Anchor.** ``server/src/Controllers/VaultController.php::validateShardEnvelopeAgainstBody`` (relaxed at line 783), call sites at ``putShard`` line 610 and ``putShardWithRoot`` line 701. Tests in ``server/tests/Vault/VaultControllerTest.php``: ``test_putShard_accepts_foreign_envelope_author_on_genesis_insert``, ``test_putShard_rejects_foreign_envelope_author_on_edit``, ``test_putShard_accepts_migration_genesis_at_arbitrary_rev``.

### 2026-05-18 — Eviction policy: age-ordered auto-purge with quota-shrink passphrase gate

**Status:** landed. Design originally in [`vault-eviction-v1.md`](../temp/finished-plans/vault-eviction-v1.md) (archived 2026-05-19); implementation collapses ``_unexpired_tombstone_candidates`` + ``_oldest_version_candidates`` into ``_next_destructive_candidate`` (single age-ordered iterator) and adds the ``mode={auto,alarm}`` parameter to ``eviction_pass``. Quota routing in ``QuotaMixin._handle_quota_exceeded`` splits into alarm (``used > quota`` → passphrase-gated cleanup), silent auto-purge (no dialog, fits the failing upload), and terminal no-history banner. Pre-existing key-name mismatch fixed in ``VaultQuotaExceededError`` — the alarm gate needs ``used_bytes`` / ``quota_bytes`` to read what the server actually emits.

**Context.** The pre-shipping eviction pipeline ran in three tiers — expired-tombstone housekeeping (always safe), unexpired-tombstone purge (destructive), oldest-version purge (destructive). Stages 2 + 3 fired automatically on 507 with only the admin-role gate (commit ``f621dc1``) between an upload and irreversible deletion of recoverable data. Spec §D9 called for adding a ``purge_secret`` passphrase prompt to the 507 dialog as a second factor. The straight "prompt every purge" reading of the spec breaks the one-click reclaim UX users expect; the alternative of accepting admin-only gating leaves the system unable to detect a relay that shrinks its own quota under a paired fleet.

**Decision.** Stages 2 + 3 merge into a single age-ordered destructive purge that auto-runs when the upload's projected bytes won't fit. The merged iterator interleaves unexpired-tombstone chunks (sorted by ``deleted_at``) and oldest-version chunks of multi-version live files (sorted by ``created_at``); the loop deletes the oldest candidate until the failing upload fits, then retries init. Bound is strictly per-upload — no batching, no slack — so a compromised path can only free as much as one upload reserves. A passphrase gate fires only on the unambiguous tampering signal ``used > quota``: the server's existing init-deny guard makes ``used > quota`` impossible under normal operation, so observing it = the relay's quota shrank below previously-stored bytes. The alarm suspends uploads, surfaces a brand-styled dialog with passphrase entry, runs a one-pass cleanup until ``used ≤ quota``, and resumes.

**What we deliberately did NOT do.** No client-side ledger of stored chunks vs server-reported ``used``. A consistently-lying relay could under-report ``used`` and avoid tripping the alarm; the architectural answer is perimeter trust + transport pinning, not client-side accounting. No proactive ``used`` / ``quota`` poll on connect — reactive on 507 is sufficient for v1; can layer proactive checks later if the lag matters. No fresh-device baseline tracking — the alarm condition is stateless (``used > quota`` at each check), so there's nothing for an attacker to spoof on first contact.

**Anchor.** Algorithm + threat model + UX comparison + test coverage in [`vault-eviction-v1.md`](../temp/finished-plans/vault-eviction-v1.md) (archived 2026-05-19). Implementation will touch ``desktop/src/vault/ops/eviction.py``, ``desktop/src/windows_vault_browser/quota.py``, ``desktop/src/vault/upload/errors.py``. Admin-role gate (``KIND_FORCED_EVICTION``) stays exactly as it landed in ``f621dc1``.

### 2026-05-18 — Scheduled-purge auto-executor stays fire-on-attended

**Status:** accepted.

**Context.** ``schedule_purge`` lets the user pre-arm a destructive vault clear at a future date. To execute it without user interaction, the desktop needs ``purge_secret`` in scope at the moment the autosync fires — but ``purge_secret`` is derived from the passphrase at unlock time and is in-memory only. Three real options: (a) generate ``purge_token_hash`` at vault-create, persist a copy in the keyring, read it during autosync; (b) push the schedule to the relay as a real ``KIND_SCHEDULED_PURGE`` row with a server-side cron; (c) leave fire-on-attended — autosync notifies; user reopens settings, completes with the recovery kit.

**Decision.** Option (c). Autosync already notifies on due purges (commit ``0b836aa``); the dialog copy is explicit about the online dependency. The user reopens Vault Settings → Danger zone when convenient and completes the purge with their kit. No new at-rest secret class (option a's keyring-persisted purge credential), no new server infrastructure (option b's cron + retention policy). The scheduled-purge feature stays useful as a "reminder + arming" tool rather than a fully autonomous trigger.

**What we deliberately did NOT do.** No keyring-persisted ``purge_secret``. The threat model gain from a long-lived at-rest purge credential is unfavourable: an attacker who exfiltrates the keyring blob can fire any scheduled purge regardless of unlock state. No server-side scheduler — keeps the relay surface area small + avoids a clock-skew failure mode where the relay fires a purge before the desktop's clock thinks the schedule is due.

**Anchor.** ``desktop/src/vault/ops/purge_schedule.py`` (existing autosync notification path), ``desktop/src/windows_vault/tab_danger.py`` (user completes manually). Tracked as resolved in ``docs/plans/unfinished.md`` §6.H1.

### 2026-05-18 — Fresh-unlock state is per-subprocess by design

**Status:** accepted.

**Context.** Spec §13 talks about a unified 15-minute idle window — once you've typed the passphrase to authorize a sensitive action, you have 15 minutes to chain another without re-prompting. The current implementation is *more conservative* than the spec: ``_last_unlock_at`` is per-process module state, so each subprocess (Settings window, Import wizard, Danger tab) re-prompts independently. Cross-subprocess composition would require persisting the timestamp somewhere — config-dir file or POSIX shm — which exposes a new at-rest signal that a malicious local process under the same uid can time an attack against.

**Decision.** Document the per-subprocess scope as the intentional v1 contract. Strictly tighter than the spec text. Sensitive ops chain within a single subprocess only (e.g. Settings → Danger reuses unlock); crossing into the Import wizard subprocess re-prompts. UX cost: ~1 extra prompt per multi-subprocess session — acceptable for the security gain.

**What we deliberately did NOT do.** No on-disk timestamp file. No POSIX shm segment. Both broaden the read surface for local-process attacks under the same uid; the spec's 15-minute window doesn't justify the new at-rest signal. The spec text needs an amendment to record this v1 tightening — flagged as a doc-only follow-up.

**Anchor.** ``desktop/src/vault/fresh_unlock.py:38-46`` (per-process ``_last_unlock_at``). Tracked as resolved in ``docs/plans/unfinished.md`` §5.M3.

### 2026-05-18 — Migration target relay URL rejects private hosts by literal-IP only

**Status:** accepted.

**Context.** ``migrationStart`` / ``migrationCommit`` persist a target relay URL into the ``vaults`` table and re-expose it through ``GET /header`` to every paired device. A malicious or misconfigured admin who owns the original relay can therefore redirect the paired fleet anywhere — including an internal service that exfiltrates the next set of encrypted payloads via auth header replay. The vault is end-to-end encrypted, so the *content* still can't be decrypted by the redirected target, but device tokens + envelope-shape side channels (sizes, timing) leak.

**Decision.** ``VaultController::rejectPrivateOrLoopbackHost`` (called from ``guardMigrationTargetRelayUrl``) refuses literal loopback, RFC 1918, link-local, and IPv6 ULA / ``::1`` addresses by default, plus the ``localhost`` and ``*.localhost`` DNS literals. Implementation uses PHP's ``FILTER_VALIDATE_IP`` with ``FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE``. Operators with a legitimate need to migrate across local URLs (paired desktop hitting ``http://127.0.0.1:4441`` on a developer rig) flip ``migrationAllowPrivateUrls: true`` in ``server/data/config.json``. The toggle is intentionally not surfaced in any UI — it's a deployment-time decision, not a per-operation one.

**What we deliberately did NOT do.** The filter does **not** resolve DNS at validation time. An admin who controls both the relay and an authoritative DNS server can still point ``relay.example.com`` at ``192.168.1.1`` and bypass the check. Config-time DNS resolution is too fragile to lean on — split-horizon resolvers, short TTLs, NXDOMAIN→NOERROR flips all turn an "approved" URL into a wrong-host one between the filter and the eventual request. The architectural answer to a malicious admin who owns DNS is perimeter trust + transport pinning, not a URL string filter. Documenting this gap explicitly so future-us doesn't reach for resolution as a "fix".

**Anchor.** ``server/src/Controllers/VaultController.php::rejectPrivateOrLoopbackHost``, ``server/src/Config.php::migrationAllowPrivateUrls``, ``server/tests/Vault/VaultControllerTest::test_migrationStart_rejects_private_url_by_default`` (covers ``localhost``, ``*.localhost``, RFC 1918 v4, ``169.254.169.254`` cloud-metadata, ``::1``, ``fc00::/7``). Landed in ``ff499e6``.

### 2026-05-17 — Vault manifest is sharded: root + per-folder shards

**Status:** accepted.

**Context.** The original ``vault_v1`` design from T0 shipped one
AEAD-encrypted manifest per vault — every edit ships the entire
vault's manifest envelope on every CAS publish, scaling
per-publish bandwidth with vault size rather than edit size. The
Phase 2 large-folder-perf work (SO-2 + SO-3, commits 8ffba34 +
a93ba08 + d49b643) reduced the per-publish *count* by 50× via
client-side batching, but each batch still ships the full
envelope, and the steady-state RAM cost of the in-memory
manifest dict still grows with the union of every folder's
file count. A multi-folder use-case (≥ 2 folders, ≥ 10k files
each) would see ~ 9 MB encrypted manifest per publish and ~
390 MiB RAM peak on a single-binding edit. Manifest sharding —
splitting the envelope along the natural folder boundary — was
the natural next step. The scoping doc
``temp/finished-plans/vault-manifest-sharding.md`` captured the eight-
phase plan; this entry records the lock.

**What we decided.** The encrypted manifest on the relay is now
two envelope kinds:

* A small **root** envelope (``dc-vault-root-v1`` schema,
  ``HKDF(info="dc-vault-v1/root")`` subkey, ``aad_root`` 76
  bytes) carrying vault-wide metadata + a per-folder pointer
  list. Each pointer carries the folder's currently-published
  ``shard_revision`` + ``shard_hash`` so a fresh client cold-
  starts with one root fetch and learns which shards exist plus
  what envelope hash to expect for each.
* One **shard** envelope per remote folder (``dc-vault-shard-v1``
  schema, ``HKDF(info="dc-vault-v1/shard")`` subkey, ``aad_shard``
  107 bytes — adds the 30-byte ``remote_folder_id``
  discriminator) carrying that folder's file entries +
  per-folder op-log tail + per-folder archived segments.

Each shard has its own CAS chain (``shard_revision`` +
``parent_shard_revision``) advanced by the per-folder
``vault_folder_shard_heads`` row on the relay. The root has a
parallel CAS chain on ``vaults.current_root_revision``. The
**primary publish path** is the atomic
``PUT /api/vaults/{id}/folders/{folder_id}/shard-with-root`` —
both writes commit in one SQLite IMMEDIATE transaction or both
abort, so a reader never sees a half-published pair. Folder-set
changes (add / remove / rename) and vault-wide policy edits
publish the root alone via ``PUT /api/vaults/{id}/root``;
retention purge passes that touch only one shard publish via
``PUT /api/vaults/{id}/folders/{folder_id}/shard`` and rely on
the next root publish to update the pointer.

**Trust anchor.** The root's encrypted plaintext stores
``shard_hash = sha256(shard_envelope_bytes)`` for every folder.
On decrypt the reader verifies the fetched shard's envelope
hash against the trusted root pointer before consuming the
shard's entries (formats §10.C). A relay-side rollback that
serves an older shard envelope (still authenticated under the
vault's AEAD key) fails this compare and surfaces as
``VaultShardHashMismatchError`` on the client — same shape as
the ``manifest_revision_floor`` rollback detection in §3.7.

**Where the chain is sealed and checked.** The shard-envelope
hash is non-deterministic (random per-publish nonce), so callers
cannot pre-compute it. ``Vault.publish_shard_with_root``
encrypts the shard first, then patches the matching pointer in
the supplied root with ``shard_hash =
sha256(shard_envelope_bytes)`` before sealing the root — the
§10.C invariant holds at the wire boundary without forcing
callers to thread the hash through manually. The standalone
``publish_folder_shard`` (used by the migration script + future
retention-only edits) returns ``(plaintext, envelope_hash)`` so
the caller can patch a separately-published root. On the read
side, ``fetch_unified_manifest`` passes each pointer's
``shard_hash`` into ``fetch_folder_shard`` as
``expected_shard_hash``; mismatch raises before AEAD decrypt.

**Why not** keep the single-envelope shape with delta
encoding? Two reasons. (1) Delta encoding requires server-side
state on the manifest body (a journal of revisions), which is
exactly the kind of plaintext-aware bookkeeping the
plaintext-blind relay can't do — the relay can't tell two
"add file F" mutations apart from the encrypted bytes. (2)
RAM on the client (one decrypted dict per active sync) scales
with the union of every folder's entries regardless of wire
shape. Sharding fixes both because the per-folder envelope is
the unit of fetch *and* the unit of in-memory load.

**Why not** key per-folder subkeys (one HKDF subkey per
``remote_folder_id``)? An attacker controlling the relay
already has every folder's shard ciphertext; the relevant
defense is the AAD-bound ``remote_folder_id`` substitution
check, which already fails closed under a single ``k_shard``
subkey. Per-folder subkeys add no defense + add a
device-grant-side compartment to track. The scoping doc's
"key compartment per shard" branch was rejected on that basis.

**Where it lands.**

* Wire spec: ``docs/protocol/vault-v1.md`` §6.4–§6.8,
  ``docs/protocol/vault-v1-formats.md`` §6.1 / §6.1a / §10.
* Test vectors: ``tests/protocol/vault-v1/root_v1.json`` +
  ``shard_v1.json`` (legacy ``manifest_v1.json`` kept as
  transitional fixture for tests that still exercise the
  pre-sharding ``Vault.publish_manifest`` path).
* Server schema: ``server/migrations/005_vault_manifest_shards.sql``
  (drops the legacy ``vault_manifests`` table, adds
  ``vault_root_manifests`` + ``vault_folder_shards`` +
  ``vault_folder_shard_heads``).
* Server controller: ``VaultController::{getRoot,putRoot,
  getShard,putShard,putShardWithRoot}``.
* Server repositories: ``VaultRootManifestsRepository``,
  ``VaultFolderShardsRepository`` (the latter owns the
  per-folder CAS head + the atomic shard-with-root
  SELECT-then-UPDATE under one BEGIN IMMEDIATE).
* Server capabilities: ``vault_root_cas_v1`` +
  ``vault_shard_cas_v1`` replace the legacy
  ``vault_manifest_cas_v1`` bit.
* Desktop crypto: ``build_root_aad`` / ``build_root_envelope``
  / ``build_shard_aad`` / ``build_shard_envelope`` in
  ``desktop/src/vault/crypto.py``.
* Desktop manifest dict: ``make_root_manifest`` /
  ``make_folder_shard`` / ``normalize_root_manifest_plaintext``
  / ``normalize_shard_plaintext`` / ``assemble_unified_manifest``
  / shard-aware entry helpers (``*_in_shard``) in
  ``desktop/src/vault/manifest.py``.
* Desktop wire layer: ``Vault.{fetch_root_manifest,
  publish_root_manifest, fetch_folder_shard,
  publish_folder_shard, publish_shard_with_root,
  fetch_unified_manifest}`` in ``desktop/src/vault/vault.py``.
* Wire-isolation tests: per-folder PUT counters in
  ``tests/protocol/test_desktop_vault_shard_wire.py`` +
  ``test_desktop_vault_binding_shard_isolation.py`` +
  ``test_desktop_vault_shard_lazy_load.py``.
* Migration script: ``temp/migrate_vault_to_shards.py`` +
  dry-run test ``test_temp_migrate_vault_to_shards.py``.

**Compatibility note.** ``vault_v1`` had never shipped (per
the operating constraints in
``temp/finished-plans/vault-manifest-sharding.md``), so the wire format
was altered in place — no compatibility shim, no deprecation
runway, no coexistence period across devices. The
developer's dev twin re-seeds its vault via the suite-start
setup. ``Vault.fetch_manifest`` / ``publish_manifest`` survive
on the client as Phase D compat shims so the protocol suite
stays green during a phased call-site migration; Phase H
removes them once every caller is shard-aware.

**Eight-phase rollout.** Commits ``204b0cd`` (Phase A — wire
spec + vectors), ``00cb3cc`` (Phase B — server schema +
endpoints), ``0f87ada`` (Phase C — client manifest model),
``6940c47`` (Phase D — client wire layer), ``364f92b`` (Phase
E — sync-engine surface + isolation tests), ``93c2701`` (Phase
F — lazy shard load acceptance test), ``e8a6b5f`` (Phase G —
migration script + dry-run), and this commit (Phase H —
cleanup + ADR).

### 2026-05-16 — Two-way Phase B batches publishes, aborts on CAS (no replay)

**Status:** accepted.

**Context.** The original Phase 2 SO-3 design (commit `a93ba08`)
batched only ``run_backup_only_cycle``; the perf plan said two-way
"probably stays single-publish for now" because of the §D4
keep-both conflict-rename detection in Phase A. After SO-2 + SO-3
shipped for backup-only, two-way bindings became disproportionately
slow: a 10 000-file two-way initial bind still did 10 000 per-op
publishes (~2 h on suite-0004 hardware) while the equivalent
backup-only bind was 20 minutes. Auditing the actual Phase A logic
showed the conflict detector runs *before* Phase B's drain on each
iteration — the per-op publish wasn't load-bearing for safety,
just the assumed simpler shape.

**Decision.** ``run_two_way_cycle`` Phase B now uses the same
batched primitives backup-only does (``_prepare_op_for_batch`` +
``_flush_batch``), with one critical policy change: batch publishes
in two-way pass ``max_retries=0``. On a CAS conflict the batch
aborts without replay; ``head`` is refetched; Phase B breaks early
and the outer iteration loop re-runs Phase A on the fresh head —
which is where §D4 keep-both / §A20 conflict-rename detection fires
for any concurrent writer's changes. The
``MAX_TWO_WAY_ITERATIONS = 4`` cap bounds pathological multi-device
contention.

The F-Y26 convergence check was extended so a CAS-failed batch
counts as "progress" even when ``new_revision == revision_at_start``
and no individual op succeeded — otherwise a spurious-conflict
iteration would exit the loop before a retry. Real-world conflicts
advance the revision so this exception rarely fires; it's a
robustness against test-injection and stale-CAS-probe noise.

**Rejected alternatives.**
- *Same blind CAS-replay as backup-only.* Would skip the §D4
  conflict-rename detection: if another writer added a new version
  at path X while our batch had an upload for path X, the replay
  would demote their version silently. Backup-only's
  last-writer-wins is by design; two-way's keep-both isn't
  optional.
- *Per-op publish for paths Phase A flagged + batched publish for
  rest.* Complex bookkeeping for a benefit that mostly matters in
  the multi-device case (which is when conflicts happen anyway).
  Single policy keeps the code small.
- *Keep two-way single-publish per the plan's deferred stance.*
  The plan was correct that conflict-rename detection was the
  hard part, but the existing Phase A solves it on each iteration
  — Phase B doesn't need to. Two-way users (likely the majority
  of paid users syncing across devices) get the same 4–6× speedup
  as backup-only.

**Anchor.** ``desktop/src/vault/binding/twoway.py``: imports the
SO-3 primitives (``_prepare_op_for_batch``, ``_flush_batch``,
``_BatchEntry``, ``PUBLISH_BATCH_SIZE``) from ``sync.py``; Phase B
drain rewritten around them with the ``max_retries=0`` policy;
``batch_failed_in_iteration`` added to the F-Y26 convergence check.
Tests: ``TwoWayBatchedPhaseBTests`` in
``tests/protocol/test_desktop_vault_binding_twoway.py`` (4 vectors:
clean batch / smaller batch_size split / one-conflict-retry-iter /
persistent-conflict-MAX_ITER-cap).

### 2026-05-16 — Vault binding cycle batches manifest publishes at K=50

**Status:** accepted.

**Context.** Suite 0004 B7 (2026-05-16) measured a 10 000-file
initial bind at **2 h 11 min** against `php -S`, with the per-op
rate decaying 8.5 → 1.3 ops/s as the encrypted manifest grew. Two
cliffs: every successful op did one redundant `fetch_manifest`
even though `publish_manifest` already returned the new manifest
(SO-2); and every file got its own CAS publish (10 000 publishes,
each shipping the full encrypted envelope = `O(N²)` bytes) (SO-3).

**Decision.** Two client-only changes on `desktop/src/vault/binding/sync.py`:

1. **SO-2**: `_execute_op` and its `_execute_upload` /
   `_execute_delete` / `_promote_to_delete` helpers now return
   `tuple[SyncOpOutcome, dict[str, Any]]` so the cycle threads the
   post-publish manifest forward without a separate GET. F-Y07
   refresh stays on `status == "failed"` (CAS conflict recovery).
2. **SO-3**: `run_backup_only_cycle` accumulates per-op chunk-PUT
   results into a `_BatchEntry` list (default `PUBLISH_BATCH_SIZE =
   50`); every K ops the batch flushes through
   `_publish_batch_with_cas_retry`, which folds the batched
   mutations onto the parent manifest with idempotent helpers
   (`add_or_append_file_version` is a no-op on duplicate
   `version_id`, tombstone helpers tolerate already-tombstoned
   paths) and runs one CAS publish. Partial batch always flushes at
   cycle-end; F-Y08 cancel still in effect → single attempt, no
   retry storm. `twoway.py` Phase B stays single-publish for now —
   conflict-rename detection in two-way inspects the manifest per
   file and is harder to batch safely.

**Rejected alternatives.**
- *Server-side batched-publish endpoint.* Would trim bandwidth
  further (delta-only) but doesn't fix the cliff; client batching
  uses the existing CAS contract.
- *Manifest sharding (per-folder envelopes).* Worth a separate
  design doc once empirical numbers show this is the limit. Real
  architectural change; not the cliff fix.
- *Batching the two-way conflict path.* The per-file conflict-
  rename detector would need refactoring into a K-op replay; too
  invasive for this pass.

**Empirical result.** 10k bind drops to **20 m 31 s** (6.4×). 1k
bind 70.4 s → 17.0 s (4.1×). Manifest publishes drop 50× as
predicted; chunk-PUT serial cost on single-threaded `php -S` now
dominates the residual. Apache mod_php (real deployment) should
land closer to the predicted ceiling.

**Anchor.** `desktop/src/vault/binding/sync.py`:`run_backup_only_cycle` /
`_BatchEntry` / `_prepare_op_for_batch` / `_apply_batch_to_manifest` /
`_publish_batch_with_cas_retry` / `_flush_batch`.
`desktop/src/vault/upload/single_file.py`:`prepare_upload_for_batch`.
Commits: `8ffba34` (SO-2), `a93ba08` (SO-3), `08401d5`
(review fixes). Tests:
`tests/protocol/test_desktop_vault_binding_batched_publish.py`,
`FetchManifestPerOpTests` in `test_desktop_vault_binding_sync.py`,
`TwoWayFetchManifestPerOpTests` in `test_desktop_vault_binding_twoway.py`.

### 2026-05-16 — Per-file dedupe stub pins `version_id` across kill-mid-batch

**Status:** accepted.

**Context.** The SO-3 plan assumed chunks would dedupe on retry
via the relay's content-addressed `chunk_id`. That premise broke
because `chunk_id = HMAC(content, version_id, index)` and
`version_id` is freshly random per `prepare_upload_for_batch`
call. Without intervention, a kill-mid-batch retry would re-encrypt
each file with a fresh `version_id`, producing fresh `chunk_id`s
and orphaning the previously-PUT chunks on the relay (~50 chunks
× per-chunk size per kill). The relay's GC eventually reclaims
orphans, but storage drift under repeated kills is observable and
the user pays for unnecessary network traffic on retry.

**Decision.** Persist a lightweight `BatchedUploadStub` per file
in `<cache_dir>/batched/<session_id>.json` keyed by `(vault_id,
remote_path, content_fingerprint)`. The stub records `entry_id`
and `version_id` allocated on the first prep attempt. On retry,
`find_matching_stub` returns the same ids → same chunk_ids →
relay's `batch_head_chunks` reports `present: True` for everything
already PUT → no re-upload. Stubs in the `batched/` subdirectory
are invisible to `list_resumable_sessions` (which scans only the
parent), so the user's resume banner is unaffected. The cycle
clears stubs after a successful batch publish; on failure they
survive for the retry. Stale stubs (content edited between
attempts) are reaped inline by `reap_stubs_for_path` when prep
allocates fresh ids. Per-binding reap on disconnect drops stubs
for that binding's dropped pending-op paths; a 14-day TTL sweep
runs once per vault open as belt-and-braces for orphans the
per-path reapers miss.

**Rejected alternatives.**
- *Deterministic `version_id` derived from `(path, fingerprint)`.*
  Would make `chunk_id` stable across runs without disk state, but
  changes manifest version-history semantics: a file edited back
  to a prior content reuses that prior version's `version_id`
  rather than appending a fresh entry, losing the audit trail of
  "the file went A → B → A".
- *Accept chunk waste; rely on GC.* Server-side GC is user-
  triggered (eviction UI), not automatic. Storage drift is
  bounded but real under churn.
- *Reuse the existing `UploadSession` JSON.* Would conflate
  single-file resume (which the banner surfaces to the user) with
  batched-cycle internals (which it shouldn't). Separate
  subdirectory + separate dataclass keeps the two namespaces
  apart.

**Anchor.** `desktop/src/vault/upload/batch_session.py` (new module:
`BatchedUploadStub`, `find_matching_stub`, `save_stub`,
`clear_stub`, `reap_stubs_for_path`, `reap_expired_stubs`,
`default_batch_cache_dir`). Used by
`desktop/src/vault/upload/single_file.py:prepare_upload_for_batch`
and cleared by `desktop/src/vault/binding/sync.py:_flush_batch`.
Reaping wired into
`desktop/src/vault/binding/lifecycle.py:disconnect_binding` and
`desktop/src/vault/binding/runtime_watchers.py:VaultWatcherRuntime.start_for_active_bindings`.
Tests: `KillMidBatchResumeTests` and `StubReuseDirectTests` in
`tests/protocol/test_desktop_vault_binding_batched_publish.py`.

### 2026-05-13 — Android delivery tracker gives up on absent rows + 12h orphan sweep

**Status:** accepted.

**Context.** `android_logs_9.txt` (2026-05-13) showed `u0a454` at 118 mAh
`mobile_radio:fgs` in a 2h 29m on-battery window — ~745 mAh / 10h
equivalent, ~10× the radio-tail-cost target. The plan's
`bf83c67`-round acceptance criteria for cancellation + skip streaks +
`poll_timeout_3000ms` were all met. The cost was hiding in 187
`delivery.tracker.skipped` events over a 3h post-clear AppLog window
that contained **zero** Android-side `transfer.upload.completed`
events. Phantom outgoing transfers from earlier test sessions
(streaming `android_logs.txt` uploads in rounds `_4`/`_5`/`_6`) sat
in Room with `delivered=0` AND active status, keeping
`getActiveDeliveryIds()` non-empty every screen-on second.

Two leaks in `PollService.runDeliveryPoll`: (1) `val s = byId[tid] ?:
continue` silently bypassed the 2-min stall safeguard when the
server's `/sent-status` no longer returned the row — evidence is the
total absence of `delivery.tracker.stall` across all 9 captured log
sessions. (2) The streaming "deliberately don't add to
`trackerGaveUp`" branch kept polling forever for streaming rows
whose `UploadWorker` was long dead.

**Decision.** Two complementary fixes, both built on pure decision
helpers in `service/DeliveryTrackerDecisions.kt` so the policy is
JVM-unit-testable.

*Fix A (runtime).* `runDeliveryPoll` now consults
`trackerAbsentDecision(prevTimestampMs, nowMs, stallTimeoutMs)` when
`/sent-status` omits a tracked tid. First absent observation seeds a
new `trackerAbsentSince` map (separate from `trackerLastProgress` so
flicker doesn't corrupt the present-row clock); subsequent ticks
within `DELIVERY_STALL_TIMEOUT_MS` (2 min) keep waiting; past the
window the tid joins in-memory `trackerGaveUp` and
`clearDeliveryProgress` zeroes the deliveryChunks/Total fields. The
present-row path resets the absent clock on observation so a
recovered row doesn't trip a false give-up.

*Fix B (startup).* New `sweepOrphanOutgoingTransfers` coroutine runs
once on `PollService.onCreate` after a 5 s settle delay. It queries
`getStaleUndeliveredOutgoing(ageThreshold)` for outgoing rows older
than `ORPHAN_SWEEP_AGE_SECONDS` (12 h) with `delivered=0` and an
active status. One `/sent-status` call decides per-row via
`orphanSweepAction(localStatus, presentInSentStatus)`: present →
leave; absent + `COMPLETE`/`SENDING` → `markDelivered` (server
pruned a finished transfer); absent + `UPLOADING`/`WAITING_STREAM` →
`markAborted reason="tracking_expired"` (server's 24h
`INCOMPLETE_EXPIRY` pruned a never-finished upload). Network/auth
failures are non-fatal; next service restart retries. 12 h was
chosen to sit above the server's longest non-delivery expiry (24h
`INCOMPLETE_EXPIRY`) by half, giving the server ample time to
resolve the row either way before our sweep decides.

**Alternatives.** (a) Persistent give-up column in Room — rejected;
in-memory `trackerGaveUp` was already the pattern for classic stall
and a new column would need a migration for negligible benefit
(Fix B already drains persistent orphans). (b) Mark all
absent-and-old rows `delivered=1` indiscriminately — rejected; a
streaming row stuck in `UPLOADING` clearly never delivered and
claiming otherwise misleads the user. (c) Server-side
"acked-and-deleted" stub returned in `/sent-status` so the client
can positively confirm delivery without inference — rejected for v1
as a bigger surface change; the local heuristic is good enough and
the server endpoint stays unchanged. (d) Per-tick `markDelivered`
inside the stall branch when status is `COMPLETE`/`SENDING` —
rejected; couples runtime tracker with the assumption-based policy
that belongs at startup boundary, not every 500 ms.

**Anchor.** `android/app/src/main/kotlin/com/desktopconnector/service/DeliveryTrackerDecisions.kt`
(pure helpers); `service/PollService.kt::runDeliveryPoll` (Fix A
wire-in at the `byId[tid] ?: continue` site, ~line 1242);
`service/PollService.kt::sweepOrphanOutgoingTransfers` (Fix B
coroutine launched from `onCreate`);
`data/QueuedTransfer.kt::getStaleUndeliveredOutgoing` (DAO query
backing the sweep);
`android/app/src/test/kotlin/com/desktopconnector/service/DeliveryTrackerDecisionsTest.kt`
(11 cases covering both helpers); `docs/plans/android-radio-tail-cost.md`
"What `_9.txt` showed" + "Changes deployed (2026-05-13)" sections.

---

### 2026-05-12 — Wrong-passphrase rate-limit is Argon2id-implicit, no counter

**Status:** accepted.

**Context.** Live-testing pass against the vault create / recovery
flows surfaced a doc-vs-code drift: `docs/plans/post-breakup-
followups.md` §3 listed "wrong-passphrase rate-limit — verify the
keyring-backed retry budget" as a live-test target. There is no
keyring-backed retry budget anywhere in `vault/recovery_kit.py` or
`windows_vault/tab_recovery.py`. The wizard's recovery-test path
re-enables the Test button after every attempt; the export-bundle
import flow at `vault/export/bundle.py` does the same.

**Decision.** v1 deliberately ships without an explicit retry counter
or lockout. The rate limit comes from Argon2id at the v1-locked
parameters (m=128 MiB, t=4, parallelism=1) inside
`derive_recovery_wrap_key`. Wall-clock cost is ~1-10 s per attempt on
typical hardware; a generated 7-word passphrase from the in-app
generator carries ≈ 84 bits of entropy. Offline brute-force is
infeasible (~10^25 attempts × 1 s/attempt at the locked params);
online attempts are bounded by physical access to the device + the
same Argon2id wall-clock floor. The "keyring-backed retry budget"
wording in the plan doc was aspirational, not implemented, and is
struck — an explicit counter buys nothing against an attacker who is
already wall-clock-bound by Argon2id.

**Alternatives.** (a) Add an explicit per-device retry counter in the
keyring with exponential backoff — rejected. Argon2id already
enforces the cost floor; a counter adds no defence against an
attacker with the kit, and the rare in-app typo path (user keeps
mistyping their own passphrase) deserves UX clarity over a lockout
that would lock the legitimate user out. (b) Track failure counts in
config.json (unencrypted) — rejected: an attacker with file-system
access bypasses it trivially, and a user who hits 5 typos in a row
shouldn't be locked out for hours. (c) Web-style CAPTCHA after N
failures — rejected as ill-fit for desktop-local crypto.

**Anchor.** `vault/crypto.py::derive_recovery_wrap_key` (the locked
Argon2id params); `vault/recovery_kit.py::verify_recovery_kit` (the
single-attempt verify surface); `windows_vault/tab_recovery.py`
(Test button re-enable, no counter). Plan §3 line reworded in
`temp/finished-plans/post-breakup-followups.md`.

### 2026-05-12 — Cross-session vault-create orphans get a local-only resume

**Status:** accepted.

**Context.** The vault create flow's four phases run prepare → save_grant
→ publish_initial → config.save. The 2026-05-07 fix (commit `eb2f71b`)
folded phases 3 + 4 into one worker call, shrinking the in-session window
between "row on relay" and "id in config.json" to microseconds. Cross-
session orphans — rows published in a wizard session that was abandoned
before config.save() (commit-failure, SIGKILL, network timeout after
server-side write succeeded) — still leak: the next wizard launch has no
knowledge of the prior vault id and creates a new one, leaving the
abandoned row on the relay forever. The dev twin's live-testing surfaced
this concretely: two `vaults` rows after one user-visible onboarding.

**Decision.** Path A: an in-config pending-publish marker plus a Resume
/ Discard UI panel on wizard launch. After `save_local_vault_grant`
returns, `config.vault.pending_publish = {vault_id, server_url, created_at}`
is persisted via `config.save()`; the same key is cleared in the
`config.save()` that writes `last_known_id` on success. Wizard launch
checks the marker — if present and the vault id doesn't match
`last_known_id`, the wizard opens on a "Resume previous attempt"
panel. Resume asks for the passphrase, re-derives fresh recovery
material (new `recovery_secret`, new `argon_salt`, new envelope) using
the existing master key read from the local grant, then either PUT-
headers the orphaned relay row (revision N → N+1, recovery rotated) or
POSTs a fresh row under the same `vault_id` if the relay 404s. Both
paths converge on the wizard's normal success screen with
`recovery_secret_bytes` populated, so Export + Verify works as if the
user had just finished a fresh create. Discard runs the existing
`feedback_security_ux` confirmation gate before deleting the local
grant + clearing the marker; the orphan stays on the relay as
unrecoverable ciphertext and falls out via retention policy.

**Alternatives.** (a) Path B from the live-testing-followup options: a
new authenticated `DELETE /api/vaults/{vault_id}` endpoint scoped to
"vault-author-on-this-device-only". Rejected: needs a server schema
addition (`vaults.created_by_device_id`), a threat-model entry for
paired-peer deletes, and bidirectional migration. Adds protocol surface
for one orphan case, with no demand from other features. The harmless-
ciphertext path is acceptable: a relay row no client holds the master
key for is byte-equivalent to a deleted row. (b) Adopt-only Resume (no
re-publish, no PUT-header) — rejected: the prior session's recovery
material is unrecoverable (random `recovery_secret` was in volatile
memory only), so an adopt without rotation leaves the user with a vault
that has no working recovery path. Rotating on Resume removes that
trap. (c) Persist the entire `_pending_publish` payload to disk so
Resume can byte-identically re-POST — rejected: the payload includes
the wrapped master key in cleartext-against-disk form (it's already AEAD
under the recovery_envelope_id key, but storing the assembled bundle
isn't necessary when the grant gives us the master key directly).

**Anchor.** `desktop/src/vault/resume.py` (marker helpers,
`complete_pending_publish`, `discard_pending_publish`,
`ResumedVaultState`); `desktop/src/vault/binding/runtime.py`
(`VaultHttpRelay.put_header`); `desktop/src/windows_vault/onboard_window.py`
(resume_or_discard / resume_passphrase / resuming stack pages,
`perform_resume`, marker set/clear in `perform_create`);
`tests/protocol/test_desktop_vault_resume.py`. Six new diagnostic events
under `vault.resume.*` catalogued in `docs/diagnostics.events.md`.

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
`temp/finished-plans/desktop-file-size-breakup.md` (original scope) +
`temp/finished-plans/post-breakup-followups.md` (wave progression). The
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
