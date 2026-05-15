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
| [desktop-connector-vault-plan-md/](plans/desktop-connector-vault-plan-md/desktop-connector-vault-00-index.md) | 11-part incremental plan to add a persistent, account-less, E2E-encrypted **Vault** with remote folders, browse-only import, versioned upload/delete, export/import bundles, and guarded sync engine. T0 decision lock + tracker live alongside the plan files. Desktop phases T0–T14/T17 landed; Android phases T15/T16 explicitly deferred. | Open |
| [post-breakup-followups.md](plans/post-breakup-followups.md) | Threads spawned by the file-size breakup. §1 (vault package consolidation) and §2 (orphan vault rows) closed; §3 live-testing roadmap stays open as the working sequencing doc. | Open (§3 only) |
| [live-testing-followup.md](plans/live-testing-followup.md) | Rolling backlog of UX/correctness items surfaced while driving the dev twin. Items 1–9 shipped; items 10+ accept new findings from un-driven vault flows (eviction, resume-after-kill, cross-device grant, large folder bind, migration switch-back, ransomware detector, scheduled purge). | Open (continuous) |
| [android-radio-tail-cost.md](plans/android-radio-tail-cost.md) | Android battery investigation: cellular-radio tail driven by phantom delivery-tracker rows. Fix A (absent-row stall safeguard) + Fix B (12 h orphan sweep) shipped 2026-05-13; awaiting `android_logs_10.txt` dumpsys to confirm `mobile_radio ≤ 70 mAh / 10 h`. | Open (awaiting empirical validation) |

Older finished plans (brand-rollout, desktop-multi-device-support,
readme_changes, desktop-file-size-breakup, vault-browser-chrome-redesign,
and the pre-vault batch) are archived under
[`temp/finished-plans/`](../temp/finished-plans/).

## Protocol reference (living docs)

| Doc | Purpose |
|------|--------|
| [protocol.md](protocol/protocol.md) | Formal spec of the HTTP + encrypted-envelope protocol between clients and relay. Reverse-specified from `main`. |
| [explain.protocol.md](protocol/explain.protocol.md) | Rationale for why `protocol.md` exists, how to extend it, how to use it during change design. |
| [protocol.compatibility.md](protocol.compatibility.md) | Per-row classification of every protocol surface change as preserving / extending / breaking. |
| [protocol.examples.md](protocol.examples.md) | Canonical request / response examples for each endpoint and mode. |
