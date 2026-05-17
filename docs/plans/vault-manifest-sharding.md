# Vault manifest sharding

Split the single per-vault encrypted manifest envelope into a small
**root** envelope (vault metadata + folder list) plus one **shard**
envelope per remote folder. Most publishes only touch one shard, so
an edit in `/docs` doesn't ship `/photos`.

**Status**: design / scoping. No implementation yet. Driven 2026-05-16
after Phase 2 (SO-2 + SO-3) shipped. The pre-condition for taking
this on is **use case B** — a multi-folder vault where the per-
publish bandwidth/RAM of the single-envelope shape becomes
load-bearing. Use case A (a single huge folder) does **not** benefit
from sharding; the per-folder shard would still be huge. Splitting
into multiple folders is the workaround for A.

**Operating constraints** (set by the user 2026-05-16):
- vault_v1 has **never shipped**; we can alter the wire format in
  place. No compatibility shim. No deprecation runway. No coexistence
  period across devices.
- Single-user, single-machine for now; the only existing data is the
  developer's test vault. A one-shot, throwaway migration script is
  acceptable.
- The migration script can live in `temp/` and be deleted after the
  user runs it once. Nothing in this work is release-worthy.

---

## Motivation

`Vault.publish_manifest` and `Vault.fetch_manifest` currently move
the whole manifest envelope for a vault on every cycle. After
SO-2 + SO-3 (`docs/plans/vault-large-folder-perf.md` §Phase 2), the
per-publish bandwidth is amortized across K=50 ops, but each
publish still ships **the entire vault's** encrypted manifest. For a
vault with multiple folders, the cost of editing one folder scales
with the *sum* of all folders' file counts.

Concretely, a vault holding:
- `/docs` — 10 000 entries
- `/photos` — 10 000 entries
- `/code` — 10 000 entries

today ships ~9 MB encrypted manifest **per publish**, regardless of
which folder is being edited. With sharding, an edit in `/docs`
ships ~3 MB; `/photos` and `/code` shards stay put.

Steady-state RAM follows the same curve: the sync engine holds the
decrypted manifest dict in memory while ops drain. At 30 k entries
across three folders, that's ~390 MiB persistently held during a
sync. Sharding lets the engine load only the binding's shard
(~130 MiB if it's the docs binding).

Single-folder vaults at any scale see no benefit — the one shard
*is* the manifest. Use cases A (single huge folder) and B
(multi-folder) diverge here. This work is for B.

---

## Architecture

Two envelope kinds per vault, both AEAD-sealed with the existing
manifest subkey:

```
Vault root envelope (small, kilobytes):
    schema:                "dc-vault-manifest-v1"
    vault_id:              <32-hex>
    root_revision:         <int, bumps on folder-set change>
    parent_root_revision:  <int>
    created_at:            <rfc3339>
    author_device_id:      <32-hex>
    manifest_format_version: 1
    retention_policy:      { keep_deleted_days, keep_versions }
    remote_folders: [
        {
            remote_folder_id:      "rf_v1_<24base32>"
            display_name_enc:      <opaque>
            created_at:            <rfc3339>
            created_by_device_id:  <32-hex>
            state:                 "active" | "deleted" | "draining"
            retention_policy:      { … }                   # per-folder override
            ignore_patterns:       [ … ]
            shard_revision:        <int, current published revision of the shard>
            shard_hash:            <hex sha256 of the shard envelope>
        }
        …
    ]
    operation_log_tail:    [ … ]    # vault-wide events: folder add/remove, retention changes
    archived_op_segments:  [ … ]

Per-folder shard envelope (potentially large, megabytes):
    schema:                  "dc-vault-shard-v1"
    vault_id:                <32-hex>
    remote_folder_id:        "rf_v1_<24base32>"
    shard_revision:          <int>
    parent_shard_revision:   <int>
    created_at:              <rfc3339>
    author_device_id:        <32-hex>
    manifest_format_version: 1
    entries: [                                            # the file entries
        { entry_id, path, deleted, latest_version_id, versions: [ … ] }
        …
    ]
    archived_op_segments:    [ … ]                        # folder-scoped events
```

**Two-level CAS.** Root revision bumps when the folder set changes
(add / remove / rename) or vault-wide policy changes. Each shard
has its own revision sequence; entries-level edits CAS only against
the relevant shard. The root tracks each shard's latest published
revision in `remote_folders[*].shard_revision` so a fresh client can
discover the per-shard state in one root fetch.

**Hash chain for trust.** The root stores `shard_hash` for each
shard. When a client publishes a new shard revision, it
*atomically* re-publishes the root with the updated
`shard_revision` + `shard_hash` for that folder. Both publishes
happen in a single API call per the wire spec (see §Phase A
below); the relay sees them as one CAS atomic. Without that, a
client could publish a shard whose hash doesn't match the root —
treated as relay tampering at decrypt time.

**AAD shape.** Both envelope kinds bind their identity into AEAD AAD:

```
Root AAD:   "dc-vault-v1/root" || vault_id || root_revision
                              || parent_root_revision || author_device_id

Shard AAD:  "dc-vault-v1/shard" || vault_id || remote_folder_id
                                || shard_revision
                                || parent_shard_revision
                                || author_device_id
```

This is byte-exact with the existing manifest-envelope shape (just
new HKDF/AAD label) and the format-version byte stays `0x01`.

---

## Phase plan

Eight phases. Each is commit-sized (half a day to ~2 days of focused
work), produces a green test suite at the end, and is independently
landable on the feature branch. Phases A → B+C → D → E → F → G → H
form a strict dependency chain after Phase A — B and C can run in
parallel.

Cumulative estimate: **8–11 days of focused work.**

### Phase A — Wire spec + test vectors (no runtime code change)

**Scope.**
- Update `docs/protocol/vault-v1-formats.md`:
  - Document the two envelope kinds (root, shard) with byte layouts.
  - Document the new AAD labels.
  - Document the hash-chain invariant (root's `shard_hash` ==
    sha256 of the shard envelope it references).
- Update `docs/protocol/vault-v1.md` (the wire spec):
  - Replace §6.4 `GET /api/vaults/{id}/manifest` and §6.5
    `PUT /api/vaults/{id}/manifest` with:
    - `GET /api/vaults/{id}/root`
    - `PUT /api/vaults/{id}/root` (CAS on root_revision)
    - `GET /api/vaults/{id}/folders/{folder_id}/shard`
    - `PUT /api/vaults/{id}/folders/{folder_id}/shard` (CAS on
      shard_revision)
    - `PUT /api/vaults/{id}/folders/{folder_id}/shard-with-root`
      (atomic publish: new shard + root update in one CAS).
  - Document the 409 `vault_shard_conflict` shape (mirrors
    `vault_manifest_conflict` but per-shard).
- Replace `tests/protocol/vault-v1/manifest_v1.json` with
  `root_v1.json` + `shard_v1.json`. Each carries plaintext input +
  AEAD ciphertext output + AAD reference + decrypt-and-verify
  expectations.
- Update `tests/protocol/vault-v1/README.md`.

**Acceptance.**
- `tests/protocol/test_desktop_vault_v1_vectors.py` (or the
  equivalent test loader) reads the new vectors and asserts shape.
- The vectors-driven cross-runtime tests still pass on whatever
  runtimes already consumed `manifest_v1.json` (likely zero today
  in this branch, since the vault is Python-only).
- `docs/protocol/vault-v1-formats.md` sections numbered, with
  cross-references to `vault-v1.md`.

**Risk.** None — spec-only.

---

### Phase B — Server schema + endpoints

**Scope.**
- New migration file `server/migrations/00X_vault_manifest_shards.sql`:
  - Drop the single `vault_manifests` row-per-vault shape.
  - Add `vault_root_manifests(vault_id, root_revision,
    parent_root_revision, envelope_blob, envelope_hash,
    published_at)`.
  - Add `vault_folder_shards(vault_id, remote_folder_id,
    shard_revision, parent_shard_revision, envelope_blob,
    envelope_hash, published_at, PRIMARY KEY (vault_id,
    remote_folder_id))`.
  - Indexes for the lookup paths.
- Update `server/src/Repositories/VaultRepository.php` (or split
  into `VaultRootRepository` + `VaultShardRepository`).
- Update `server/src/Controllers/VaultController.php` to add the
  four new endpoints (GET/PUT root, GET/PUT shard) plus the atomic
  `PUT /folders/{id}/shard-with-root`.
- Per-shard CAS: 409 `vault_shard_conflict` includes the
  `current_shard_revision` + `current_shard_envelope_ciphertext`
  inline (mirrors `vault_manifest_conflict`'s inline-envelope
  shape).
- Update `server/src/Capabilities.php` / `VaultCapabilities.php`:
  drop `vault_manifest_cas_v1`, add `vault_shard_cas_v1` and
  `vault_root_cas_v1`.
- Delete the old `/api/vaults/{id}/manifest` endpoint outright.

**Acceptance.**
- New PHP-side tests under `server/tests/` (or whatever the project
  uses) cover:
  - Root CAS round-trip (publish → fetch → publish again with stale
    revision → 409).
  - Shard CAS round-trip per folder; CAS conflicts are scoped per
    shard.
  - Atomic shard-with-root publish: either both land or neither.
  - 410 / 404 on the dropped `/manifest` endpoint.
- `GET /api/health` `capabilities` reflects the new capability set.
- Existing transfer tests still pass (vault is the only subsystem
  touched).

**Risk.** Schema migration drops data — fine because the feature
branch has no prod use. The repo's `server/data/connector.db` on
the developer's machine will need a wipe, which the
suite-start step in `docs/testing/vault-tests.md` already does.

---

### Phase C — Client manifest model (no wire yet)

**Scope.**
- In `desktop/src/vault/manifest.py`:
  - Rename current `make_manifest` → `make_root_manifest` (limited
    to root-level fields: folder list, retention, op log).
  - Add `make_folder_shard` builder.
  - Add `normalize_root_manifest_plaintext` and
    `normalize_shard_plaintext` (mirror the current
    `normalize_manifest_plaintext` shape).
  - Add `canonical_root_json` / `canonical_shard_json`.
- In `desktop/src/vault/crypto.py`:
  - Add `build_root_aad` and `build_shard_aad`.
  - Add `build_root_envelope` and `build_shard_envelope`.
- Move the entry-level helpers (`find_file_entry`,
  `add_or_append_file_version`, `tombstone_file_entry`,
  `restore_file_entry`, `merge_with_remote_head`,
  `tombstone_files_under`) to operate on a shard dict, not the
  legacy vault-wide manifest dict.
- Add an `assemble_unified_manifest(root, shards_by_id)` helper that
  produces the *legacy-shaped* dict (one big `remote_folders` array
  with all entries) for callers that haven't been ported yet.
  Provides a soft migration surface for phases D + E.

**Acceptance.**
- New unit tests in
  `tests/protocol/test_desktop_vault_manifest.py`:
  - Build root, encrypt with `build_root_envelope`, decrypt,
    round-trip equal.
  - Build shard, encrypt, decrypt, round-trip equal.
  - Both envelope kinds reject mismatched AAD (wrong revision,
    wrong folder_id).
  - `assemble_unified_manifest` produces the same shape the
    pre-sharding `fetch_manifest` returned for the same data.
- Entry-level helper tests (the existing ones) pass against the new
  per-shard signature.

**Risk.** The legacy entry helpers are used widely. Carry both
signatures during this phase if needed; phase D + E migrate
callers.

---

### Phase D — Client wire layer

**Scope.**
- In `desktop/src/vault/vault.py`:
  - Replace `Vault.fetch_manifest` + `Vault.publish_manifest` with
    four new methods:
    - `Vault.fetch_root_manifest(relay) -> dict`
    - `Vault.publish_root_manifest(relay, root) -> dict`
    - `Vault.fetch_folder_shard(relay, folder_id) -> dict`
    - `Vault.publish_folder_shard(relay, folder_id, shard) -> dict`
  - Add `Vault.publish_shard_with_root(relay, folder_id, shard,
    root)` for the atomic shard+root update.
- Add a compatibility method:
  - `Vault.fetch_unified_manifest(relay) -> dict` — fetches the
    root, fetches every shard it lists, calls
    `assemble_unified_manifest`. Use only from call sites that
    haven't been ported to shard-aware fetching.
- Update the `SyncVault` protocol in `binding/sync.py` to expose
  the shard-aware fetch/publish methods.

**Acceptance.**
- A new `FakeShardedRelay` test double in
  `tests/protocol/test_desktop_vault_relay.py` (or a new file):
  - Stores root + N shard envelopes.
  - Returns 409 `vault_shard_conflict` on stale shard CAS.
  - Returns 409 `vault_manifest_conflict` on stale root CAS.
  - Supports the atomic `shard-with-root` path (both succeed or
    both 409).
- `tests/protocol/test_desktop_vault_v1_round_trip.py` (or the
  closest existing equivalent) updates to drive both envelope kinds
  through encrypt-publish-fetch-decrypt.
- `Vault.fetch_unified_manifest` returns the same shape pre-sharding
  callers expect (golden-file comparison).

**Risk.** Atomic shard-with-root needs careful CAS implementation
on the server side (two SQLite writes in one transaction). Server
tests in Phase B already cover the atomicity contract.

---

### Phase E — Sync engine (binding cycles)

**Scope.**
- `desktop/src/vault/binding/sync.py`:
  - `run_backup_only_cycle` calls
    `Vault.fetch_folder_shard(binding.remote_folder_id)` instead of
    `fetch_manifest`.
  - `_publish_batch_with_cas_retry` publishes via
    `publish_shard_with_root` so the root's `shard_revision` +
    `shard_hash` stay in sync atomically.
  - `_log_batch_cas_steamrolls` reads from the shard's entries
    (currently walks the vault-wide `remote_folders` list).
  - `_apply_batch_to_manifest` operates on a shard, not the
    vault-wide manifest.
- `desktop/src/vault/binding/twoway.py`:
  - `_apply_remote_to_local` reads only the binding's shard, not
    every remote_folder.
  - Phase A's "ghost reaping" (F-Y20) scopes to the shard.
- `desktop/src/vault/binding/preflight.py`:
  - `count_manifest_entries` walks only the relevant shard (or
    sums all shards on demand, depending on what the estimator
    needs).
  - Estimator constants re-fit against per-shard sizes (not
    vault-wide).
- Update `desktop/src/vault/upload/single_file.py`'s
  `prepare_upload_for_batch` to read/write a shard instead of a
  unified manifest.

**Acceptance.**
- Every test in
  `tests/protocol/test_desktop_vault_binding_sync.py` /
  `…_twoway.py` / `…_cancellation.py` /
  `…_batched_publish.py` passes against the new shard-aware
  cycles.
- New tests in `tests/protocol/test_desktop_vault_binding_sync.py`:
  - **Shard isolation**: two bindings to different folders of the
    same vault sync concurrently; one binding's publish does
    *not* touch the other's shard. Asserts via a probe relay that
    counts per-shard PUTs.
  - **Cross-shard idempotence**: a CAS conflict on shard A does
    not invalidate ops queued for shard B.
- `tests/protocol/test_desktop_vault_binding_preflight.py` updates
  for the per-shard estimator.

**Risk.** This is the load-bearing phase — most active code touches
the sync engine. Land it on its own branch, run the full suite +
the live B7 driver (`/tmp/dc-b7-syncone.py`) before pushing.

---

### Phase F — Cross-shard operations

**Scope.**
- `desktop/src/vault/integrity.py`:
  - Vault-wide integrity walk: iterate root's
    `remote_folders`, fetch each shard, audit entries against
    referenced chunks. Single-binding integrity restricts to one
    shard.
- `desktop/src/vault/ops/eviction.py`:
  - GC plan computation: walk every shard to gather referenced
    chunks. Per-vault scope unchanged; the walk is the change.
- `desktop/src/vault/ui/browser_model.py`:
  - Lazy-load shards as the user opens folder views. The browser
    currently materializes the whole manifest up front — change
    to a `BrowserModel(vault)` whose folder open hooks
    `fetch_folder_shard`.
- `desktop/src/vault/export.py` / `import_/runner.py`:
  - Export bundle: write root + every shard as separate
    encrypted blobs.
  - Import: replay root + shards into the target vault.
- `desktop/src/vault/folder/actions.py` (folder add / remove / rename):
  - "Add folder" publishes only the root with a new entry +
    creates an empty shard.
  - "Remove folder" tombstones the root entry; the shard remains
    until retention purge.
- `desktop/src/vault/ops/clear.py`:
  - "Clear folder" tombstones every entry in the relevant shard +
    bumps shard revision; root unchanged.
  - "Clear vault" tombstones every shard's entries + bumps every
    shard's revision (one CAS per shard).
- `desktop/src/vault/ops/delete.py`:
  - Restore from tombstone — shard-scoped CAS only.

**Acceptance.**
- All existing tests under `tests/protocol/test_desktop_vault_*`
  that exercise these ops pass against the shard-aware paths:
  - `test_desktop_vault_integrity.py`
  - `test_desktop_vault_eviction.py`
  - `test_desktop_vault_browser_model.py`
  - `test_desktop_vault_export.py` /
    `test_desktop_vault_import.py`
  - `test_desktop_vault_folder_actions.py` /
    `test_desktop_vault_folders.py`
  - `test_desktop_vault_clear.py`
  - `test_desktop_vault_delete.py` /
    `test_desktop_vault_restore.py`
- New test in
  `tests/protocol/test_desktop_vault_browser_model.py`:
  - **Lazy shard load**: opening one folder fetches only that
    folder's shard, not all of them. Counted via a probe relay.

**Risk.** Many call sites touched. Mitigate by holding
`fetch_unified_manifest` as the soft surface until every call
site is ported one-by-one, then drop it.

---

### Phase G — One-shot migration script

**Scope.**
- New script at `temp/migrate_vault_to_shards.py` (deliberately not
  under `desktop/src/` because it's throwaway).
- Reads the developer's existing v1 single-envelope manifest from
  the relay (using the *old* endpoint definition, kept available
  via a build flag or commented-out code path during this phase).
- Splits the manifest plaintext: vault-wide fields → root; each
  `remote_folders[i]` → shard for that folder.
- Encrypts each envelope.
- Publishes via the new endpoints in order:
  1. Publish each shard.
  2. Publish the root with the new
     `remote_folders[*].shard_revision` + `shard_hash` references.
- Atomicity isn't a hard requirement (single-user, no concurrent
  writers), but the script is idempotent: re-running after a
  partial completion uses the per-shard CAS to detect already-
  migrated shards and skip them.

**Acceptance.**
- Manual run against the developer's test vault produces a working
  sharded vault.
- A scripted dry-run test
  (`tests/protocol/test_temp_migrate_vault_to_shards.py`) builds a
  fake v1 manifest in-memory, runs the migration logic, asserts
  the resulting root + shards round-trip equal to the source
  entries.

**Risk.** Throwaway code, low test bar. The acceptance test is more
of a confidence check than a regression net.

---

### Phase H — Cleanup

**Scope.**
- Remove the `fetch_unified_manifest` compatibility surface from
  `Vault` (every caller should be shard-aware by now).
- Remove the old endpoint paths from any temporarily-retained
  client code.
- Drop the migration script from `temp/` once you've run it.
- Update `CLAUDE.md` Vault section: bullet pointing at the new
  manifest structure.
- Update `docs/vault-architecture.md` (the canonical reference) to
  describe the root + shard architecture.
- Add a dated entry to `docs/architecture-decisions.md`.
- Bump nothing in version.json / capabilities (this is a wire
  change but the vault never shipped).

**Acceptance.**
- `grep -r "fetch_unified_manifest" desktop/src/` returns no hits.
- `grep -r "publish_manifest\b" desktop/src/` returns no hits in
  active code paths.
- Full vault suite green.
- ADR entry committed.

**Risk.** None — purely housekeeping.

---

## Risks + open questions

**1. Folder rename hot spot.** Renaming a folder bumps the root
revision, not a shard's. Concurrent edits in that folder during a
rename win a CAS race: the rename publish (root) succeeds, then the
ongoing edit's `publish_shard_with_root` fails because root_revision
moved. The client retries on the new root. Acceptable but worth
covering with a test.

**2. Shard discovery cold start.** A fresh client needs to fetch the
root before it can know which shards exist. That's one extra GET
on the first sync per vault session. Cached in `VaultLocalIndex` so
subsequent syncs don't repeat.

**3. Vault-wide quota.** Server-side per-vault quota currently sums
chunk storage across the single manifest. Sharded: each shard
references some chunks; the union is the quota footprint. The
existing `vault_chunks` table already keys by `vault_id` so the
quota math doesn't change — just the manifest-traversal code on
the GC plan side.

**4. AAD label collision check.** The new `dc-vault-v1/root` and
`dc-vault-v1/shard` HKDF/AAD labels are distinct from the existing
`dc-vault-v1/manifest`, `…/chunk`, `…/header`,
`…/recovery-envelope`, `…/content-fingerprint`,
`…/chunk-nonce`, `…/device-grant`, `…/export-bundle`. Verify by
grep before Phase C lands.

**5. Hash-chain trust.** The root's `shard_hash` is the trust
anchor for shard contents. If an attacker controls the relay and
serves an old shard envelope (rollback within a single shard),
the AEAD verifies (still our key), but the hash won't match what
the trusted root said. Decrypt path **must** verify
`sha256(shard_envelope) == root.remote_folders[i].shard_hash`
before consuming the entries. Same trust shape as the existing
manifest_revision_floor anti-rollback in §3.7.

**6. Migration timing.** The user has one test vault. Migration is
a single ~minute script run. No coordination needed. **But**
between the migration run and the next sync, any concurrent
writer would see inconsistent state (old endpoint gone, new ones
not yet known to that client). Single-user means there are no
concurrent writers; document this assumption explicitly in the
migration script's banner.

**7. Test vector regeneration runtime.** The cross-runtime test
vectors are designed to be readable by future runtimes (Kotlin,
PHP). Even though this branch is Python-only today, the vectors
are part of the spec — regenerate them correctly so a future
Kotlin implementation has a north star.

---

## Out of scope

- **Sub-folder sharding inside a single shard** (e.g., chunk-of-
  entries shards within `/photos`). Would help use case A (single
  huge folder), but the engineering complexity is significantly
  higher (entry routing, balancing). Defer until use case A is the
  load-bearing pain.
- **Cross-shard transactions** (e.g., "move file from /docs to
  /photos atomically"). The existing v1 doesn't support this either
  (it's a delete + add internally); sharding doesn't change that.
- **Differential shard fetches** (e.g., "give me the delta since
  shard_revision N"). Would shrink wire size for large shards but
  needs a server-side journal of shard revisions. The Phase 2 K=50
  batching already amortizes per-edit publishes; differential
  fetches are the *next* optimization, not this one.

---

## Status: Infrastructure done, production port in progress

**Infrastructure done 2026-05-17** on ``tresor-vault``. Eight
commits land the phases A through H end-to-end as infrastructure
(wire surface, server schema, client crypto, helpers, acceptance
tests, migration script, docs):

* ``204b0cd`` — Phase A: wire spec + test vectors. Replaced the
  single ``manifest_v1.json`` with ``root_v1.json`` +
  ``shard_v1.json``; updated ``docs/protocol/vault-v1.md`` §6.4–
  §6.8 + formats §10 with the new envelope layouts, AAD shapes
  (root 76 bytes / shard 107 bytes), and the §10.C hash chain.
  Added ``build_root_aad`` / ``build_shard_aad`` / matching
  envelope builders in ``desktop/src/vault/crypto.py``.
* ``00cb3cc`` — Phase B: server schema + endpoints.
  ``005_vault_manifest_shards.sql`` drops ``vault_manifests``,
  adds ``vault_root_manifests`` + ``vault_folder_shards`` +
  ``vault_folder_shard_heads``. Two new repositories
  (``VaultRootManifestsRepository``,
  ``VaultFolderShardsRepository`` — the latter owns the atomic
  SELECT-then-UPDATE shard-with-root path). Six new controller
  methods replacing the legacy ``get/putManifest`` pair. New
  error envelopes (``VaultRootConflictError``,
  ``VaultShardConflictError``, ``VaultShardRootConflictError``,
  ``VaultRootTamperedError``, ``VaultShardTamperedError``).
* ``0f87ada`` — Phase C: shard-aware client manifest model.
  ``make_root_manifest`` / ``make_folder_shard`` + normalizers
  + ``assemble_unified_manifest`` for soft migration +
  shard-scoped entry helpers (``*_in_shard``) in
  ``desktop/src/vault/manifest.py``.
* ``6940c47`` — Phase D: shard-aware Vault wire methods.
  ``fetch_root_manifest`` / ``publish_root_manifest`` /
  ``fetch_folder_shard`` / ``publish_folder_shard`` /
  ``publish_shard_with_root`` / ``fetch_unified_manifest``.
  ``Vault.fetch_manifest`` / ``publish_manifest`` stay on the
  class as compat shims; production ``VaultHttpRelay``'s legacy
  methods are explicit ``NotImplementedError`` stubs so callers
  surface a clear "migrate me" message instead of a 404.
* ``364f92b`` — Phase E: ``SyncVault`` protocol grows the
  shard-aware methods + per-folder ``count_shard_entries``;
  acceptance tests demonstrate shard isolation (per-folder
  PUTs) + cross-shard idempotence (CAS conflict on shard A
  doesn't break shard B's queued publish).
* ``93c2701`` — Phase F: lazy shard load acceptance test at the
  Vault wire layer. Opening one folder fetches only its shard;
  the unified-manifest compat path's vault-wide fetch is
  contrasted explicitly.
* ``e8a6b5f`` — Phase G: ``temp/migrate_vault_to_shards.py``
  (decompose helper + idempotent driver) + dry-run test
  exercising it against ``FakeShardedRelay``.
* (this commit) — Phase H: ``CLAUDE.md`` Vault section + this
  doc + ``docs/vault-architecture.md`` + dated ADR entry.

**Deferred work flagged for visibility.** Phases E + F's plan
called for porting every legacy ``vault.fetch_manifest`` /
``vault.publish_manifest`` call site (sync engine, integrity,
eviction, browser_model, export/import, folder/actions,
ops/clear, ops/delete) off the compat shims. That mechanical
migration is left for a follow-up commit — the wire surface +
acceptance tests in this rollout pin the contract those
call sites will switch to, and the legacy compat path keeps
every existing test passing during the transition. Phase H's
suite gate is fully green via the compat path. Plan-§H's
``grep -r "fetch_unified_manifest"`` returning no hits is the
follow-up commit's responsibility; same for the
``publish_manifest`` removal.

### Plan A: production port

Post-infrastructure commits on ``tresor-vault`` setting up the
mechanical port, then doing it one production source file at a
time. Each commit leaves the suite green (Python 1564 + PHP
260).

* ``24bee55`` — fix §10.C shard hash chain end-to-end (review-
  driven follow-up; landed before the port began).
* ``bbdabf1`` — prep infrastructure for the port:
  ``Vault.decrypt_root_envelope`` / ``decrypt_shard_envelope``,
  ``VaultCASConflictError`` shard/root accessors,
  ``FakeUploadRelay``'s sharded surface, and
  ``seed_sharded_state_from_manifest``.
* ``2bb5e6e`` — option 2a: restored the legacy server endpoints
  + production ``VaultHttpRelay.get_manifest`` /
  ``put_manifest`` + reverted ``Vault.create_new`` to legacy
  manifest, so the new desktop installs cleanly against the new
  server while the production port is in flight.

Remaining Plan A checklist:

* [x] **Step 1** (2026-05-17): ported ``prepare_upload_for_batch``
  in ``desktop/src/vault/upload/single_file.py`` to take a
  per-folder ``shard`` instead of the unified manifest. Sole
  caller ``binding/sync.py:_prepare_upload_for_batch`` adapted
  with a 4-line shard-synthesis shim that disappeared in step 2.
* [x] **Step 2** (2026-05-17): ported ``binding/sync.py``'s
  ``run_backup_only_cycle`` to fetch root + folder shard via
  ``_fetch_folder_state`` (§10.C hash chain check + genesis
  shard synthesis), publish via ``publish_shard_with_root``,
  and handle shard/root-conflict envelopes in CAS retry.
  ``binding/twoway.py``'s Phase B drain rides the same shared
  helpers; Phase A still walks a unified-manifest ``head``
  synthesized in-memory from the post-publish sharded state so
  step 3 can port it cleanly. Step 1's ``_shard_view`` shim is
  gone — sync now hands ``state.shard`` to
  ``prepare_upload_for_batch`` directly. Test surface migrated
  to sharded counters (``published_shards``,
  ``shard_with_root_puts``); ``BatchProbeRelay`` +
  ``_TwoWayBatchProbeRelay`` learned to inject shard-CAS
  conflicts. Multi-device tests gain a transitional
  ``mirror_legacy_from_sharded`` helper that re-publishes the
  unified mirror after each cycle so device-B's two-way Phase A
  sees device-A's sharded writes.
* [x] **Step 3** (2026-05-17): ported ``binding/twoway.py``'s
  Phase A (``_apply_remote_to_local`` + ``_apply_remote_upsert``)
  to take a ``_BindingFolderState`` instead of the unified
  manifest. ``run_two_way_cycle``'s legacy ``head`` variable is
  gone — both phases consume ``state``. Step 2's post-flush
  ``assemble_unified_manifest`` head-synthesis is removed; the
  end-of-iter ``head = vault.fetch_manifest(relay)`` is replaced
  by ``state = _fetch_folder_state(...)``. Dead ``_find_folder``
  helper deleted. The legacy ``download_latest_file`` call inside
  ``_apply_remote_upsert`` still wants a unified manifest, so we
  synthesize one inline from ``(state.root, {folder: state.shard})``
  — this last legacy wart goes when step 6 ports the download
  path. ``mirror_legacy_from_sharded`` test helper survives for
  the single h7 spot where mid-test ``upload_file`` still uses
  the legacy publish path (removed in step 4 when ``upload_file``
  is ported). h7's ``_decrypt_head`` now synthesizes the unified
  view from sharded relay state so assertions read the
  authoritative source.
* [x] **Step 4** (2026-05-17): ported ``upload/single_file.py``'s
  ``upload_file`` + ``_publish_with_cas_retry``, ``upload/folder.py``,
  and ``upload/resume.py`` to publish via ``publish_shard_with_root``.
  New shared module ``upload/folder_state.py`` (``FolderState``,
  ``fetch_folder_state``, ``find_root_folder_pointer``) avoids the
  circular import between ``binding/sync.py`` and the upload module.
  ``UploadVault`` Protocol gains the sharded methods
  (``fetch_root_manifest`` / ``fetch_folder_shard`` /
  ``publish_shard_with_root`` / ``decrypt_root_envelope`` /
  ``decrypt_shard_envelope``); the legacy ``fetch_manifest`` /
  ``publish_manifest`` pair stays for callers that haven't ported
  yet (Protocol narrowed in step 7). ``UploadResult.manifest`` stays
  populated — synthesized from the post-publish ``(root, shard)``
  via ``assemble_unified_manifest`` — for ~30 caller sites that
  still read it. The rename to ``.root`` + ``.shard`` lands in
  step 7's cleanup. New ``manifest.merge_local_version_into_shard``
  helper holds the §D4 tie-break / collision-rename logic that
  ``merge_with_remote_head`` provides for the legacy path; the
  upload's CAS-retry loop uses it on conflict, the initial attempt
  uses ``add_or_append_file_version_in_shard`` (blind append —
  matches pre-Phase-H semantics). New ``vault.folder_upload.cas_retry``
  + ``vault.upload.cas_retry`` events catalogued.

  Test surface: every ``FakeUploadRelay(manifest=...)`` site across
  ~10 test files now seeds both surfaces (legacy via
  ``vault.publish_manifest`` + ``relay.current_revision`` reset,
  sharded via ``seed_sharded_state_from_manifest``).
  ``mirror_legacy_from_sharded`` is the recipe for tests where a
  not-yet-ported path (baseline / delete / eviction / export /
  migration_runner) reads the legacy mirror after a sharded upload;
  it advances ``relay.current_revision`` by exactly one per call so
  legacy-revision-count assertions stay readable. Two §D4 acceptance
  tests (``test_concurrent_new_file_at_same_path_renames_imported``,
  ``test_two_device_concurrent_upload_tie_break_by_device_hash``)
  use the new ``parent_state`` kwarg on ``upload_file`` to inject
  a stale pre-A snapshot so device B's publish actually hits CAS
  and runs the merge path — production callers never set it.
* [x] **Step 5** (2026-05-17, partial): ported ``ops/delete.py``
  (``delete_file`` / ``delete_folder_contents`` /
  ``restore_version_to_current``) to publish via
  ``publish_shard_with_root``. New private
  ``_publish_shard_with_retry`` mirrors the upload module's CAS-retry
  shape — decrypts shard/root conflict envelopes, re-runs the op,
  retries. ``DeleteVault`` Protocol gains the sharded methods
  alongside the legacy pair (full narrow waits for step 7). The
  returned ``published`` dict is now a synthesized unified manifest
  built from the post-publish ``(root, shard)`` for caller compat.
  New event tag ``vault.delete.cas_retry`` catalogued.
  ``ops/eviction.py`` + ``ops/integrity.py`` stay on the legacy
  publish/fetch path for now — eviction's multi-folder atomic
  semantics don't fit ``publish_shard_with_root``'s one-folder
  scope cleanly, and integrity is read-only with tests built on
  trivial mocks (low port value). Both are kept coherent in tests
  via ``mirror_legacy_from_sharded`` after each sharded mutation;
  the production port lives in a future step (or step 7 cleanup if
  the eviction op is reshaped to walk shards directly).
* [x] **Step 6** (2026-05-17, no-op): deferred to step 7. ``folder/
  runtime.py``, ``import_/runner.py``, ``ui/browser_model.py``, and
  ``remote_folders.py`` all stay on the legacy ``fetch_manifest`` /
  ``publish_manifest`` path for now. Their callers' tests keep them
  coherent with the sharded surface via ``mirror_legacy_from_sharded``
  (steps 4–5 pattern). Pragmatic reasons:
  - ``remote_folders.py`` mutates only the root folder-pointer set
    — a clean ``publish_root_manifest`` port — but the test
    ``test_desktop_vault_folders.py`` uses a hand-rolled
    ``FakeManifestRelay`` that only supports the legacy
    ``get_manifest`` / ``put_manifest`` surface, and the test
    asserts on ``put_calls[0]["expected_current_revision"]`` /
    ``["manifest_hash"]``. Porting the production code without also
    porting that bespoke fake (~100 lines of CAS-tracking) breaks
    every assertion.
  - ``folder/runtime.py`` is read-only and surfaces ``fetch_manifest``
    on a high-level facade (``VaultFolderRuntime``); its callers
    expect a unified manifest dict.
  - ``ui/browser_model.py`` is a pure-function manifest renderer
    (``list_folder`` / ``decrypt_manifest``); 1 legacy ref is just
    ``find_file_entry`` used in ``detect_path_conflict``.
  - ``import_/runner.py`` is the vault export → vault import flow;
    it composes ``upload_file`` (already sharded post-step-4) +
    ``publish_manifest`` for the merged manifest. Tests already use
    ``mirror_legacy_from_sharded`` post-step-4 to keep it coherent.

  These ports become straightforward once the legacy path is REMOVED
  in step 7 — at that point there's no legacy publish for the
  ``FakeManifestRelay``-style fakes to assert on, so the test
  surface naturally migrates. Step 7's cleanup absorbs the
  production-side ports as a single pass.
* [ ] Step 7: cleanup — drop the legacy server table +
  endpoints + ``VaultHttpRelay.get_manifest`` /
  ``put_manifest`` + ``Vault.fetch_manifest`` /
  ``publish_manifest`` shims + ``assemble_unified_manifest`` +
  ``fetch_unified_manifest`` + legacy ``make_manifest`` +
  ``temp/migrate_vault_to_shards.py`` + ``manifest_v1.json``
  fixture. Flip ``Vault.create_new`` back to writing a root
  envelope.

  ### Step 7 sub-tasks (~5 commits worth)

  Step 7 is genuinely large — its surface analysis on 2026-05-17
  found 8 production files still on the legacy API, 9 test files
  using the transitional ``mirror_legacy_from_sharded`` bridge,
  one bespoke test fake (``FakeManifestRelay`` in
  ``test_desktop_vault_folders.py``) tied to the legacy CAS shape,
  and the server-side migration / table / endpoints. Each is
  pulled in below as a separate commit so progress lands
  incrementally instead of in one untested mega-diff.

  * [x] **7a** (2026-05-17) — Production: ``remote_folders.py`` +
    ``FakeManifestRelay``. ``add_remote_folder`` /
    ``rename_remote_folder`` / ``update_remote_folder_settings``
    now mutate the root folder-pointer set via
    ``publish_root_manifest``; the shared private
    ``_mutate_root_and_publish`` runs the fetch → mutate → bump →
    publish path and refreshes the local-index folder cache from
    the published root. Returns a synthesized unified manifest
    (with empty per-folder shard views) so callers' ``result["revision"]``
    / ``result["remote_folders"]`` reads keep working.
    ``FakeManifestRelay`` was rewritten as ``FakeRootRelay`` with
    only ``get_root`` / ``put_root`` (legacy ``put_manifest`` is
    gone — nothing in this test file needs it anymore). The three
    tests in ``test_desktop_vault_folders.py`` are rewritten to
    seed an empty genesis root via ``vault.publish_root_manifest``
    and to assert on root-revision counters
    (``put_root_calls[0]["expected_current_root_revision"]`` etc.).
    Genesis-root + add = revision 2; rename = revision 3 — the
    chain math is cleaner than the legacy vector-seeded variant.
    The unrelated ``manifest-v1-legacy-no-remote-folders`` test
    vector is no longer referenced from this file.

  * [x] **7b** (2026-05-17) — Production: ``folder/runtime.py``,
    ``ui/browser_model.py``. ``VaultRuntime.fetch_manifest`` and
    ``run_initial_baseline`` now go through
    ``vault.fetch_unified_manifest`` (root + per-folder shards,
    assembled). ``vault.fetch_unified_manifest`` itself disappears
    in step 7f. ``ui/browser_model.py`` needed no port — it's
    just helpers (``decrypt_manifest`` envelope decoder,
    ``list_folder`` renderer, ``detect_path_conflict``); no
    publish/fetch calls. The ``FakeVault`` in
    ``test_desktop_vault_folder_runtime.py`` was renamed to
    ``fetch_unified_manifest`` to match.

  * **7c — Production: ``import_/runner.py``.** The vault export →
    vault import flow. Reads source vault state, writes to target
    vault. Currently composes ``upload_file`` (already sharded
    post-step 4) with ``publish_manifest`` for the merged
    manifest. Port the publish to ``publish_root_manifest`` for
    the folder-set merge + ``publish_shard_with_root`` for any
    per-folder entry merges.

  * **7d — Production: ``ops/eviction.py``.** The tricky one.
    Stage 1 (expired tombstones) can span multiple folders; that
    no longer fits ``publish_shard_with_root``'s one-folder scope
    cleanly. Two design options:
    (1) Per-affected-folder publishes — loses the atomic-stage
        semantics but is the only fit for the current wire
        surface; stages become "publish N shards in sequence";
        partial failures leave a coherent vault (each shard
        publish is its own CAS).
    (2) A new ``publish_multi_shard_with_root`` endpoint — cleaner
        but adds server-side scope.
    Option 1 is the pragmatic call. Stage 2 + 3 are already
    single-folder; just port them via the same pattern as ops/
    delete.py.

  * **7e — Production: ``ops/integrity.py``.** Read-only.
    ``run_quick_check`` and ``run_full_check`` use
    ``vault.fetch_manifest`` to grab the head + walk chunk refs;
    port to ``fetch_root_manifest`` + iterate all
    ``fetch_folder_shard`` calls + assemble. ``run_full_check``'s
    historical-revision walk (``relay.list_manifest_revisions``)
    needs a sharded equivalent ``list_root_revisions`` +
    ``list_shard_revisions`` — possibly new server endpoints, or
    a flag to skip historical scope until the server side lands.

  * **7f — Vault class + tests + server: nuke the legacy surface.**
    Once 7a–7e land:
    - Drop ``Vault.fetch_manifest`` / ``publish_manifest`` /
      ``decrypt_manifest`` shims (the latter is the
      envelope-only helper still used by tests — keep that one,
      since it's the public envelope decoder).
    - Drop the legacy fields from ``UploadVault`` / ``DeleteVault``
      / ``IntegrityVault`` Protocols — narrow them.
    - Drop ``assemble_unified_manifest`` (no more synthesis
      needed once UploadResult / FolderUploadResult expose
      ``.root`` + ``.shard`` directly — that field rename is
      part of this commit). Drop ``fetch_unified_manifest`` from
      ``Vault``.
    - Drop legacy ``make_manifest`` /
      ``add_remote_folder`` (manifest helper) /
      ``rename_remote_folder`` / ``tombstone_file_entry`` /
      ``add_or_append_file_version`` /
      ``merge_with_remote_head`` from
      ``desktop/src/vault/manifest.py`` — the sharded ``_in_shard``
      variants stay.
    - Drop the test-side ``seed_sharded_state_from_manifest`` +
      ``mirror_legacy_from_sharded`` helpers in
      ``test_desktop_vault_upload.py`` — they're only needed
      while the legacy mirror exists. Tests that called them
      either drop those calls (sharded-only) or migrate to a
      pure sharded ``seed_sharded_state(vault, relay, *,
      remote_folders=[...])`` that doesn't take a unified
      manifest.
    - Drop ``relay.put_manifest`` / ``get_manifest`` from
      ``FakeUploadRelay`` (and the dup in
      ``test_desktop_vault_folders.py``).
    - Server: drop ``vault_manifests`` table + the
      ``GET/PUT /api/vaults/{id}/manifest`` endpoints +
      ``Vault.create_new``'s legacy manifest write — flip back
      to root + empty-folders genesis-root.
    - Drop ``temp/migrate_vault_to_shards.py`` +
      ``tests/protocol/test_temp_migrate_vault_to_shards.py`` +
      ``tests/protocol/vault-v1/manifest_v1.json`` once nothing
      references them.

  ### Out-of-scope until next phase

  The legacy ``vault-v1`` cross-runtime test vectors live at
  ``tests/protocol/vault-v1/*.json``. ``manifest_v1.json``
  encodes the single-manifest envelope shape — the
  ``test_vault_v1_vectors.py`` runner walks all of them. Step 7f
  removes the legacy fixture + its runner case but leaves
  ``root_v1.json`` and ``shard_v1.json`` (the sharded
  successors, landed in Phase A). Other runtimes will adopt the
  same vectors when they port.
