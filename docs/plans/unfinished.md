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

## 3. §A15 ransomware banner is dead code in the UI

Suite 0007 Test B4 (2026-05-19) ran the live ransomware-detector trip and found that the detector + pause core work as designed (binding flips to `state=paused` within ~1 s of a 100-file rename burst, no tombstone bleed during the paused window), but **none of the §A15 banner copy reaches the user**.

`vault/diagnostics/ransomware_detector.py` exports `BANNER_TITLE = "Suspicious mass change detected"`, `BANNER_BODY = "Vault sync has been paused for this folder. Review changes before uploading."`, and the four §A15 actions `ACTION_REVIEW / ACTION_ROLLBACK / ACTION_RESUME / ACTION_KEEP_PAUSED`. `vault/binding/runtime_watchers.py:248-260` logs the title+body on trip and exposes a `set_ransomware_callback(...)` hook — but no caller anywhere in the tree calls `set_ransomware_callback`. The callback stays `None`, the if-not-None branch is dead, and no UI surface renders the banner copy or the four-action affordance.

What the user sees instead (post-trip, captured via AT-SPI in `temp/automation-tests-results/0007/test-B4/`):
- The bound folder's sidebar subtitle changes from "backup-only" to "paused" (one word, no explanation).
- The binding row's "Sync now" button is replaced with a generic "Resume" play-button.
- That's it — indistinguishable from a user manually clicking the overflow "Pause sync" action.

Threat-model implication: a user returning to a ransomware-hit folder sees only a generic "paused" indicator, likely clicks Resume, and the malicious encrypted blobs immediately flow to the relay as fresh uploads (catch-up scan re-enqueues them as new files), plus tombstones fire for whichever originals were synced before pause — destroying the only safe copies on the relay.

Two resolution paths:
- **(a) Minimum viable** — wire `set_ransomware_callback` from `tray/vault_submenu.py` (alongside the existing `VaultWatcherRuntime` construction near line 228) to `self.platform.notifications.notify(title=BANNER_TITLE, body=BANNER_BODY)`. ~10 lines; surfaces a notify-send toast on every trip. Doesn't carry the four §A15 actions but gets the user's attention.
- **(b) Full §A15** — render an `Adw.Banner` on the bound folder's detail pane (or on the Sync safety panel, which is currently a deliberate placeholder) when the binding is paused for a ransomware reason. Requires a new column on `vault_bindings` (e.g. `pause_reason ∈ {user, ransomware}`) since the current schema doesn't preserve why a pause happened; without it the UI can't tell a user-pause from a detector-pause. The four §A15 buttons (Review / Rollback / Resume / Keep paused) land underneath; Resume already exists, Review/Rollback/Keep paused need new lifecycle helpers.

Detailed evidence: [`temp/automation-tests-results/0007/test-B4/result.md`](../../temp/automation-tests-results/0007/test-B4/result.md) Finding 1.

---

## 4. Source of truth references

- **Max-effort review fixes landed:** [`temp/finished-plans/max-review-result.md`](../../temp/finished-plans/max-review-result.md) — every fixed item has a strikethrough heading + commit SHA + Approach paragraph.
- **Max-effort review fix log:** [`temp/finished-plans/max-review-result-progress.md`](../../temp/finished-plans/max-review-result-progress.md).
- **Manifest-sharding plan:** [`temp/finished-plans/vault-manifest-sharding.md`](../../temp/finished-plans/vault-manifest-sharding.md) — phases A → 7f done; §3.8 (the last residual unified-shape helpers in `manifest.py`) landed 2026-05-19.
- **Architecture decisions:** [`docs/architecture-decisions.md`](../architecture-decisions.md) — 2026-05-18 entries for §6.H1 (scheduled-purge auto-executor stays fire-on-attended) and §5.M3 (per-subprocess fresh-unlock is the v1 contract) record the two "decided, no code work" closures.
