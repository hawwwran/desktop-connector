# Live testing — un-driven flow backlog

Items §1–§14 (everything found-and-fixed while driving the dev twin from 2026-05-07 through 2026-05-19) moved to [`temp/finished-plans/live-testing-followup.partly.md`](../../temp/finished-plans/live-testing-followup.partly.md) on 2026-05-19. What stays here is the still-un-driven backlog from the chained `docs/testing/vault-tests.md` suite.

When a flow lands a finding, write it up in the partly archive under the next §N heading using the Symptom / Cause / Fix shape / Acceptance / Status template, and strike the **B#** bullet from this list. Keep the identifier visible in the writeup so cross-doc references stay resolvable.

Last reconciled 2026-05-19.

---

## Backlog — un-driven flows

Candidate live-test sessions that aren't yet in the chained `docs/testing/vault-tests.md` suite. Each is one focused session against the dev twin.

Migrated 2026-05-15 from the archived [`temp/finished-plans/post-breakup-followups.md`](../../temp/finished-plans/post-breakup-followups.md) §3.

Ordered by priority (2026-05-16). Tier 1 = exercises core daily flows that block real use if broken. Tier 2 = silent-data-loss risk if a correctness path misbehaves. Tier 3 = ops/support/edge-case flows that matter but aren't day-one blockers. Identifiers descend down the list: **B6 is highest priority, B1 is lowest**.

Closed so far: B8 (resume-after-kill — partly §12), B7 (large folder bind — partly §13), B3 (migration genesis-leg — partly §14), B5 (eviction — partly §15, partial PASS). Wrong-passphrase rate-limit closed as partly §7 on 2026-05-12 (the protection is Argon2id-intrinsic; ADR at [`docs/architecture-decisions.md#2026-05-12`](../architecture-decisions.md)).

### Tier 1 — core daily flows

*(empty — both closed)*

### Tier 2 — silent data-loss risk

- **B6 — Concurrent edits with binding sync.** Edit the same file on both devices between syncs; verify the conflict-rename path (`vault/binding/twoway.py`) produces predictable output and the Activity tab logs both branches. Highest-frequency data-loss vector.
- **B4 — Ransomware detector trip.** Simulate a mass-rewrite event in a bound folder; verify `vault/binding/ransomware_detector.py` pauses sync and surfaces the warning. Last-line safety net against worst-case cloud-replication of ransomware damage.
- **B5 follow-up — full GUI eviction flow + `eviction_pass` walk.** Genesis 507 emission + triage routing closed as partly §15 (PARTIAL PASS); the alarm-dialog passphrase prompt + actual destructive purge step needs AT-SPI driving of the Vault Browser, and `eviction_pass` against real state needs SO-2 (auth limit) untied so a sync completes through the shard publish.

### Tier 3 — ops / support / edge cases

- **B2 — Debug bundle on a real install.** Generate a bundle, inspect the contents, confirm no plaintext / no keys / no tokens leak per the logging policy in CLAUDE.md. Complements partly §9's code-side leak-scan widening with a live-install spot check.
- **B1 — Schedule purge.** Set a purge schedule, fast-forward time (mock `_now_rfc3339` if needed), verify the scheduled purge fires and audits correctly.
- **B3 follow-up — full switch-back leg.** Genesis-leg landed as partly §14; the B→A switch-back leg is deferred (each leg burns the per-vault auth budget; running both consecutively trips `vault_rate_limited`). Now unblocked by the `vaultAuthLimit` config knob ([ADR 2026-05-19](../architecture-decisions.md)) — bump the cap above the floor of 10 for the test run, or add a ~60s sleep between legs. Not blocking — switch-back propagation is independently covered by unit-level tests.

Pickup recipes for B3 / B2 / B5 / B4 / B1 live in [`docs/plans/skipped-while-autonomous.md`](skipped-while-autonomous.md).
