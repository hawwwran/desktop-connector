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
| [unfinished.md](plans/unfinished.md) | Vault v1 follow-up index — open items the max-effort review pass didn't close. After the 2026-05-19 reconciliation just §3.L4 (permanent-failure UI for sync ops) and §3.8 (residual unified-shape helpers in `manifest.py`) remain. | Open |
| [live-testing-followup.md](plans/live-testing-followup.md) | Un-driven flow backlog from the chained `docs/testing/vault-tests.md` suite: B6 (concurrent edits), B5 (eviction), B4 (ransomware), B2 (debug bundle), B1 (schedule purge), plus B3 full switch-back leg. Pickup recipes live in [`skipped-while-autonomous.md`](plans/skipped-while-autonomous.md). Completed items §1–§14 archived at [`live-testing-followup.partly.md`](../temp/finished-plans/live-testing-followup.partly.md). | Open (continuous) |
| [skipped-while-autonomous.md](plans/skipped-while-autonomous.md) | Register of items skipped during unattended sessions because they need user input or design decisions: §3.L4 permanent-failure UI, §6.L5 subprocess crash detection, B6 concurrent edits, Android radio tail cost (awaits dumpsys), webcam QR scanning, live tests B2/B5/B4/B1, migration wizard dogtail drive. | Open |
| [android-radio-tail-cost.md](plans/android-radio-tail-cost.md) | Android battery investigation: cellular-radio tail driven by phantom delivery-tracker rows. Fix A (absent-row stall safeguard) + Fix B (12 h orphan sweep) shipped 2026-05-13; awaiting `android_logs_10.txt` dumpsys to confirm `mobile_radio ≤ 70 mAh / 10 h`. | Open (awaiting empirical validation) |

The canonical Vault reference now lives at [`vault-architecture.md`](vault-architecture.md);
it replaces the 16-file `desktop-connector-vault-plan-md/` directory
that previously sat under `plans/`. The original plan files (T0 lock,
critical-risks doc, per-phase plans 01–11, progress tracker) are
archived under
[`../temp/finished-plans/desktop-connector-vault-plan-md/`](../temp/finished-plans/desktop-connector-vault-plan-md/)
for decision rationale; read the architecture doc first.

Older finished plans archived under [`temp/finished-plans/`](../temp/finished-plans/) — the 2026-05-19 sweep moved `vault-open-items.md`, `vault-eviction-v1.md`, `vault-large-folder-perf.md`, and `vault-v1-build-items.md` (all four fully done), plus split off the completed halves of `live-testing-followup.md` and `skipped-while-autonomous.md` as `.partly.md` companions.

## Protocol reference (living docs)

| Doc | Purpose |
|------|--------|
| [protocol.md](protocol/protocol.md) | Formal spec of the HTTP + encrypted-envelope protocol between clients and relay. Reverse-specified from `main`. |
| [explain.protocol.md](protocol/explain.protocol.md) | Rationale for why `protocol.md` exists, how to extend it, how to use it during change design. |
| [protocol.compatibility.md](protocol.compatibility.md) | Per-row classification of every protocol surface change as preserving / extending / breaking. |
| [protocol.examples.md](protocol.examples.md) | Canonical request / response examples for each endpoint and mode. |
