# Vault v1 — outstanding follow-ups

Tracks plan items that did not land as a code fix. The Critical / High / Medium queue closed in the 2026-05-18 max-effort review pass; everything below is residue. Detail inlined so you don't have to cross-reference the archives.

Last reconciled 2026-05-19.

---

## 1. §3.L4 — Permanent-failure UI for retry-exhausted sync ops

`MAX_OP_ATTEMPTS=10` ops sit in the queue forever; no permanent-failure UI surface today. Needs UI scoping (banner + per-op detail row + queue inspector). Reviewer flagged it as a UX rough edge rather than a v1 blocker on 2026-05-18; skipped autonomously.

---

## 2. Manifest-sharding §3.8 — Residual unified-shape helpers in `manifest.py`

The 7f cleanup sweep on 2026-05-19 dropped `make_manifest` / `make_remote_folder` / `find_file_entry` / `tombstone_file_entry` (see [`temp/finished-plans/vault-manifest-sharding.md`](../../temp/finished-plans/vault-manifest-sharding.md) §3.5). Two unified-shape helpers in `manifest.py` survived with **zero production callers**:

- `tombstone_files_under(manifest, *, remote_folder_id, path_prefix, deleted_at, author_device_id)` — bulk soft-delete on the unified manifest. Shard equivalent `tombstone_files_under_in_shard` (line 1208 of `manifest.py`) is the production path; the legacy variant is exercised only by `test_desktop_vault_delete.py:test_tombstone_files_under_*`.
- `restore_file_entry(manifest, *, remote_folder_id, path, new_version, author_device_id)` — single-entry restore on the unified manifest. Shard equivalent `restore_file_entry_in_shard` is at `manifest.py:1379`; only `test_desktop_vault_delete.py:test_restore_file_entry_*` calls the legacy form.

Both helpers shipped before the sharded migration completed; the §3.5 drop list was kept narrow (review feedback: don't expand scope mid-sweep). Following the same migration shape — extract the legacy helpers from their two test files via the shard variant + entry-splicing — would let `manifest.py` drop both functions cleanly.

**Why this is a follow-up, not a §3.5 sub-item**: the migration is mechanical (each test file has at most 4 callsites) but `tombstone_files_under` returns a `tuple[dict, list[str]]` (manifest + list of tombstoned paths), and the shard variant `tombstone_files_under_in_shard` returns the same shape against a shard. The test-side splice needs to reflect the entries change back into the unified dict — same pattern as `_apply_tombstone_in_unified` in `test_desktop_vault_delete.py` but for bulk paths. Single-commit refactor; ~80 lines of test code change + ~60 lines of `manifest.py` removed.

**Suggested resolution**: extract once into a shared helper (`tests/protocol/_vault_helpers.py:apply_tombstone_files_under_in_unified` and `restore_in_unified`), migrate the two test files, drop the unified-shape helpers from `manifest.py`.

---

## 3. Source of truth references

- **Max-effort review fixes landed:** [`temp/finished-plans/max-review-result.md`](../../temp/finished-plans/max-review-result.md) — every fixed item has a strikethrough heading + commit SHA + Approach paragraph.
- **Max-effort review fix log:** [`temp/finished-plans/max-review-result-progress.md`](../../temp/finished-plans/max-review-result-progress.md).
- **Manifest-sharding plan:** [`temp/finished-plans/vault-manifest-sharding.md`](../../temp/finished-plans/vault-manifest-sharding.md) — phases A → 7f done; §2 of this file is the last residue.
- **Architecture decisions:** [`docs/architecture-decisions.md`](../architecture-decisions.md) — 2026-05-18 entries for §6.H1 (scheduled-purge auto-executor stays fire-on-attended) and §5.M3 (per-subprocess fresh-unlock is the v1 contract) record the two "decided, no code work" closures.
