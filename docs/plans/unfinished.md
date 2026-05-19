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

## ~~3. §A15 ransomware banner is dead code in the UI~~ — FIXED 2026-05-19

Wired the notify-send toast in `tray/vault_submenu.py` so a detector trip surfaces a `dialog-warning` notification with the §A15 banner title + body. Verified live via `dbus-monitor` — the notification daemon receives the call ~25 ms after the trip. Unit tests pin the callback contract (fires once on trip, idempotent on re-trip, runtime survives a throwing callback). The fuller §A15 four-action affordance (Review / Rollback / Resume / Keep paused as an Adw.Banner) is deferred — the notify-send path closes the practical "user has no idea their vault is paused for a reason" gap.

Resolution writeup: [`temp/automation-tests-results/0007/test-fixes/result.md`](../../temp/automation-tests-results/0007/test-fixes/result.md).

Original finding follows for context.

---

Suite 0007 Test B4 (2026-05-19) ran the live ransomware-detector trip and found that the detector + pause core work as designed (binding flips to `state=paused` within ~1 s of a 100-file rename burst, no tombstone bleed during the paused window), but **none of the §A15 banner copy reaches the user**.

`vault/diagnostics/ransomware_detector.py` exports `BANNER_TITLE = "Suspicious mass change detected"`, `BANNER_BODY = "Vault sync has been paused for this folder. Review changes before uploading."`, and the four §A15 actions `ACTION_REVIEW / ACTION_ROLLBACK / ACTION_RESUME / ACTION_KEEP_PAUSED`. `vault/binding/runtime_watchers.py:251-260` logs the title+body on trip and exposes a `set_ransomware_callback(...)` hook — but no caller anywhere in the tree calls `set_ransomware_callback`. The callback stays `None`, the if-not-None branch is dead, and no UI surface renders the banner copy or the four-action affordance.

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

## ~~4. Debug bundle is missing 2 of 5 advertised entries (Maintenance tab caller bug)~~ — FIXED 2026-05-19

Extracted `collect_bundle_inputs(config_data, config_dir, vault_id_undashed)` as a module-level helper in `windows_vault/tab_maintenance.py`; the worker now passes all 5 inputs to `write_debug_bundle`. Both new entries use local-DB-only sources (no relay calls, no AEAD decryption), so the bundle stays generatable on a locked vault. 7 unit tests pin the helper's contract. Live re-bundle confirmed all 5 entries present, leak scan still clean.

Resolution writeup: [`temp/automation-tests-results/0007/test-fixes/result.md`](../../temp/automation-tests-results/0007/test-fixes/result.md).

Original finding follows for context.

---

Suite 0007 Test B2 (2026-05-19) ran the live debug-bundle leak scan. The redaction layer works — auth_token, private_key:pem body, vault_grant, Bearer pattern, base64-32-byte runs all returned 0 matches in the produced bundle. But the bundle itself is **producer-side incomplete**.

The Maintenance tab's own description label (visible in the UI) promises: "Packages a redacted snapshot of vault config, local index schema, **binding states**, and the tail of the vault.log file into a ZIP." So this isn't just a docstring drift — the user-facing copy advertises `binding_states` and the bundle doesn't deliver them.

`vault/diagnostics/debug_bundle.py:219-272` (`build_debug_bundle_bytes`) accepts 5 optional named inputs — `config`, `db_path`, `binding_states`, `activity_log_path`, `manifest_summary` — and the module docstring (lines 12-22) lists all 5 as the intended outputs. The builder skips each input if `None`, so a caller omitting an input gets a silently-smaller bundle.

The only UI caller (`windows_vault/tab_maintenance.py:92-99`) passes only 3 — `config`, `db_path`, `activity_log_path`. `binding_states` and `manifest_summary` aren't computed or threaded through, so every bundle a user can produce is missing those two entries:

```python
out = write_debug_bundle(
    destination,
    config=config_dump,
    db_path=local_index.db_path,
    activity_log_path=(activity_log if activity_log.exists() else None),
)
```

No error or diagnostic fires; the bundle is just smaller than the docstring claims it should be. Impact: a support engineer reading the bundle gets no visibility into per-binding state (binding_id / state / sync_mode / last_synced_revision) — so "why isn't this folder syncing?" requires a separate state dump — and no visibility into per-vault revision + chunk_count + retained-history totals.

Resolution: ~25 lines in `tab_maintenance.py:worker()` to compute `binding_states` via `VaultBindingsStore.list_bindings()` and `manifest_summary` by opening the local vault from grant (gracefully handle the locked-vault case). Detailed sketch in [`temp/automation-tests-results/0007/test-B2/result.md`](../../temp/automation-tests-results/0007/test-B2/result.md) Finding 1.

---

## 5. Source of truth references

- **Max-effort review fixes landed:** [`temp/finished-plans/max-review-result.md`](../../temp/finished-plans/max-review-result.md) — every fixed item has a strikethrough heading + commit SHA + Approach paragraph.
- **Max-effort review fix log:** [`temp/finished-plans/max-review-result-progress.md`](../../temp/finished-plans/max-review-result-progress.md).
- **Manifest-sharding plan:** [`temp/finished-plans/vault-manifest-sharding.md`](../../temp/finished-plans/vault-manifest-sharding.md) — phases A → 7f done; §3.8 (the last residual unified-shape helpers in `manifest.py`) landed 2026-05-19.
- **Architecture decisions:** [`docs/architecture-decisions.md`](../architecture-decisions.md) — 2026-05-18 entries for §6.H1 (scheduled-purge auto-executor stays fire-on-attended) and §5.M3 (per-subprocess fresh-unlock is the v1 contract) record the two "decided, no code work" closures.
