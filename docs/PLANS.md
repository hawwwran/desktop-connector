# Plans Index

Working notes under [`plans/`](./plans/) (tracked in git; small,
current set). Protocol reference docs under
[`protocol/`](./protocol/). Release runbooks under
[`release/`](./release/).

## Status legend
- **Open** — implementation not started, or in progress
- **Done** — implementation landed; any remaining work is explicitly listed
  as follow-up in the plan
- **Reference** — living spec, never "done"; updated as the
  surface changes

---

## Plans

| Plan | Intent | Status |
|------|--------|--------|
| [vault-open-items.md](plans/vault-open-items.md) | Vault v1 unblocked 2026-05-15. Evaluation gate at [`vault-critical-risks-evaluation.md`](vault-critical-risks-evaluation.md) reads 0 Open / 1 Mitigated after the two follow-ups landed: §3.7 (rollback detection) via [`plans/live-testing-followup.md`](plans/live-testing-followup.md) §10 + §3.9 / §3.11 (fresh-unlock enforcement) via §11. Remaining Mitigated entry (§3.3 — Devices tab UX + per-role server gates) is deferred post-v1. Architecture overview at [`vault-architecture.md`](vault-architecture.md). | Done |
| [live-testing-followup.md](plans/live-testing-followup.md) | Rolling backlog of UX/correctness items surfaced while driving the dev twin. Items 1–9 shipped; items 10+ accept new findings from the Backlog section's un-driven vault flows (eviction, resume-after-kill, cross-device grant, concurrent edits, large folder bind, migration switch-back, ransomware detector, scheduled purge, debug bundle on a real install). | Open (continuous) |
| [android-radio-tail-cost.md](plans/android-radio-tail-cost.md) | Android battery investigation: cellular-radio tail driven by phantom delivery-tracker rows. Fix A (absent-row stall safeguard) + Fix B (12 h orphan sweep) shipped 2026-05-13; awaiting `android_logs_10.txt` dumpsys to confirm `mobile_radio ≤ 70 mAh / 10 h`. | Open (awaiting empirical validation) |
| [vault-large-folder-perf.md](plans/vault-large-folder-perf.md) | Two-phase fix for the B7 cliff (suite 0004, 10k files = 2 h 11 min). Phase 1: pre-bind warning + estimate so users aren't surprised. Phase 2: SO-2 drop redundant per-op `fetch_manifest` (~2× win) + SO-3 batched manifest publish (~50× win combined). | Open |

The canonical Vault reference now lives at [`vault-architecture.md`](vault-architecture.md);
it replaces the 16-file `desktop-connector-vault-plan-md/` directory
that previously sat under `plans/`. The original plan files (T0 lock,
critical-risks doc, per-phase plans 01–11, progress tracker) are
archived under
[`../temp/finished-plans/desktop-connector-vault-plan-md/`](../temp/finished-plans/desktop-connector-vault-plan-md/)
for decision rationale; read the architecture doc first.

Older finished plans (brand-rollout, desktop-multi-device-support,
readme_changes, desktop-file-size-breakup, vault-browser-chrome-redesign,
post-breakup-followups, and the pre-vault batch) are archived under
[`temp/finished-plans/`](../temp/finished-plans/).

## Protocol reference (living docs)

| Doc | Purpose |
|------|--------|
| [protocol.md](protocol/protocol.md) | Formal spec of the HTTP + encrypted-envelope protocol between clients and relay. Reverse-specified from `main`. |
| [explain.protocol.md](protocol/explain.protocol.md) | Rationale for why `protocol.md` exists, how to extend it, how to use it during change design. |
| [protocol.compatibility.md](protocol.compatibility.md) | Per-row classification of every protocol surface change as preserving / extending / breaking. |
| [protocol.examples.md](protocol.examples.md) | Canonical request / response examples for each endpoint and mode. |
