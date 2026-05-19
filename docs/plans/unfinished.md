# Vault v1 — outstanding follow-ups

Tracks plan items that did not land as a code fix. The Critical / High / Medium queue closed in the 2026-05-18 max-effort review pass; everything below is residue. Detail inlined so you don't have to cross-reference the archives.

Last reconciled 2026-05-19.

---

## 1. §3.L4 — Permanent-failure UI for retry-exhausted sync ops

`MAX_OP_ATTEMPTS=10` ops sit in the queue forever; no permanent-failure UI surface today. Needs UI scoping (banner + per-op detail row + queue inspector). Reviewer flagged it as a UX rough edge rather than a v1 blocker on 2026-05-18; skipped autonomously.

---

## 2. Folder display-name cache leak after lock-by-grant-deletion

Suite 0006 Test 09 (2026-05-19) found that deleting the keyring vault grant doesn't invalidate `vault_remote_folders_cache.display_name` in the local index DB at `~/.config/desktop-connector/vault-local-index.sqlite3`. After lock, a fresh vault-browser window renders folder names from cache in plaintext even with no grant in keyring. File contents + filenames are intact (encrypted shard, decrypted lazily); only folder display names leak.

Two resolution paths:
- **(a) Doc fix.** Clarify the threat-model wording in `docs/testing/vault-tests.md` Test 09 — file content/names are the protected invariant; folder display names are cached-by-design for fast list rendering.
- **(b) Code fix.** Wipe `vault_remote_folders_cache` rows for the vault on grant deletion. The cache rebuilds on next unlock from the decrypted root manifest, so the cost is one extra round of decryption per lock cycle.

Threat-model decision is the user's; the leak is real either way. Tracked here so a future review picks it up.

---

## 3. Source of truth references

- **Max-effort review fixes landed:** [`temp/finished-plans/max-review-result.md`](../../temp/finished-plans/max-review-result.md) — every fixed item has a strikethrough heading + commit SHA + Approach paragraph.
- **Max-effort review fix log:** [`temp/finished-plans/max-review-result-progress.md`](../../temp/finished-plans/max-review-result-progress.md).
- **Manifest-sharding plan:** [`temp/finished-plans/vault-manifest-sharding.md`](../../temp/finished-plans/vault-manifest-sharding.md) — phases A → 7f done; §3.8 (the last residual unified-shape helpers in `manifest.py`) landed 2026-05-19.
- **Architecture decisions:** [`docs/architecture-decisions.md`](../architecture-decisions.md) — 2026-05-18 entries for §6.H1 (scheduled-purge auto-executor stays fire-on-attended) and §5.M3 (per-subprocess fresh-unlock is the v1 contract) record the two "decided, no code work" closures.
