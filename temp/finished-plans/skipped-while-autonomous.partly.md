# Skipped while running autonomously — completed parts

Carved out of `docs/plans/skipped-while-autonomous.md` on 2026-05-19 once the B3 live test was driven against real PHP relays. The unfinished half of that file stays in `docs/plans/`.

---

## B3 — Migration switch-back live test *(done 2026-05-19)*

**Status:** the genesis-leg migration round-trip ran against two hermetic PHP relays. Test landed as `tests/protocol/test_desktop_vault_migration_live.py` (commit `253fad0`). Findings written up as §14 in [`live-testing-followup.partly.md`](live-testing-followup.partly.md). The full B→A switch-back leg is deferred — each leg burns the per-vault auth budget; running both consecutively trips `vault_rate_limited`. Independently covered by unit-level tests.

**Original entry follows (preserved for the historical record):**

> The §5.C1 migration wizard (commit `f6b04feXX` series, 2026-05-18) is pinned by source tests (`tests/protocol/test_desktop_vault_migration_wizard_source.py`) and engine tests (`tests/protocol/test_desktop_vault_migration_runner.py`); the live drive against two real PHP relays remains the only thing not exercised.
>
> **Setup recipe:**
> ```bash
> cd /home/mhavranek/git/desktop-connector
>
> # Wipe + start relay A on 4441 (canonical dev relay).
> rm -f server/data/connector.db
> rm -rf server/storage/* server/data/logs
> mkdir -p server/data/logs server/storage
> php -S 127.0.0.1:4441 -t server/public/ > server/data/logs/relay-a.log 2>&1 &
> echo $! > /tmp/relay-a.pid
>
> # Spin a second relay on 4442 with an isolated storage tree.
> mkdir -p /tmp/dc-relay-b/data/logs /tmp/dc-relay-b/storage
> cp -r server/public /tmp/dc-relay-b/
> # Reuse the same schema; relay B starts empty.
> php -S 127.0.0.1:4442 -t /tmp/dc-relay-b/public/ > /tmp/dc-relay-b/data/logs/relay-b.log 2>&1 &
> echo $! > /tmp/relay-b.pid
> ```
>
> **Test flow:**
> 1. Spin dev twin against relay A: `cd desktop && DC_ALLOW_MULTI_INSTANCE=1 python3 -m src.main --config-dir=~/.config/desktop-connector-dev --server-url=http://127.0.0.1:4441`.
> 2. Onboard a fresh vault. Add the `~/Documents/dc-dev-test-folder/` binding with 3 test files. Wait for upload to land (Activity tab shows green).
> 3. Open Vault Settings → Migration → "Migrate to another relay…". Enter `http://127.0.0.1:4442`. Drive setup → confirm → progress → done.
> 4. Verify all 3 files landed on relay B: `sqlite3 /tmp/dc-relay-b/data/connector.db "SELECT vault_id, root_revision FROM vault_roots"`. Should show the same root_revision as relay A had at the end of step 2.
> 5. Verify the Migration tab in Vault Settings shows the "switch back to relay A" affordance (visible during the post-commit grace window).
> 6. Click "Switch back". Drive the wizard back. Verify A's root_revision is one higher than B's (the switch-back republishes).
> 7. To verify the §5.M6 grace-window cleanup: edit `~/.config/desktop-connector-dev/config.json`, set `vault_previous_relay_expires_at` to a past RFC3339 timestamp, reopen the Migration tab. The switch-back affordance should be gone.
>
> **Write up findings as §14 in `docs/plans/live-testing-followup.md`** using the Symptom / Cause / Fix shape / Acceptance / Status template (matches §1–§13).
