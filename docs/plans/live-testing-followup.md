# Live testing — un-driven flow backlog

Items §1–§14 (everything found-and-fixed while driving the dev twin from 2026-05-07 through 2026-05-19) moved to [`temp/finished-plans/live-testing-followup.partly.md`](../../temp/finished-plans/live-testing-followup.partly.md) on 2026-05-19. What stays here is the still-un-driven backlog from the chained `docs/testing/vault-tests.md` suite.

When a flow lands a finding, write it up in the partly archive under the next §N heading using the Symptom / Cause / Fix shape / Acceptance / Status template, and strike the **B#** bullet from this list. Keep the identifier visible in the writeup so cross-doc references stay resolvable.

Last reconciled 2026-05-19.

---

## Backlog — un-driven flows

Candidate live-test sessions that aren't yet in the chained `docs/testing/vault-tests.md` suite. Each is one focused session against the dev twin.

Migrated 2026-05-15 from the archived [`temp/finished-plans/post-breakup-followups.md`](../../temp/finished-plans/post-breakup-followups.md) §3.

Ordered by priority (2026-05-16). Tier 1 = exercises core daily flows that block real use if broken. Tier 2 = silent-data-loss risk if a correctness path misbehaves. Tier 3 = ops/support/edge-case flows that matter but aren't day-one blockers. Identifiers descend down the list: **B6 is highest priority, B1 is lowest**.

Closed so far: B8 (resume-after-kill — partly §12), B7 (large folder bind — partly §13), B3 (migration genesis-leg — partly §14; switch-back leg — partly §17), B5 (eviction — partly §15 partial PASS + partly §16 algorithm walk PASS), B4 (ransomware detector trip — suite 0007 test-B4 + fix landed in `tresor-vault@f6aaf93`), B2 (debug bundle leak scan + producer-gap fix — suite 0007 test-B2 + fix landed in `tresor-vault@f6aaf93`), B1 (schedule purge — suite 0007 test-B1 + fix landed in `tresor-vault@a9816ee`). Wrong-passphrase rate-limit closed as partly §7 on 2026-05-12 (the protection is Argon2id-intrinsic; ADR at [`docs/architecture-decisions.md#2026-05-12`](../architecture-decisions.md)).

### Tier 1 — core daily flows

*(empty — both closed)*

### Tier 2 — silent data-loss risk

- **B6 — Concurrent edits with binding sync.** Edit the same file on both devices between syncs; verify the conflict-rename path (`vault/binding/twoway.py`) produces predictable output and the Activity tab logs both branches. Highest-frequency data-loss vector.
- **B5 GUI follow-up — alarm dialog + passphrase prompt drive.** Eviction algorithm walk closed as partly §16; the remaining gap is AT-SPI driving the Vault Browser's `_handle_quota_exceeded` alarm dialog (passphrase entry, cleanup-then-resume). Not blocking — algorithm itself is now proven correct against real state.

### Tier 3 — ops / support / edge cases

*(empty — B3 follow-up closed as partly §17 on 2026-05-19)*

Pickup recipes for B5 / B6 live in [`docs/plans/skipped-while-autonomous.md`](skipped-while-autonomous.md). B4 / B2 / B1 recipes there are historical — those tests are closed.
