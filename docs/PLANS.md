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
| [android-radio-tail-cost.md](plans/android-radio-tail-cost.md) | Android battery investigation: cellular-radio tail driven by phantom delivery-tracker rows. Fix A (absent-row stall safeguard) + Fix B (12 h orphan sweep) shipped 2026-05-13; awaiting `android_logs_10.txt` dumpsys to confirm `mobile_radio ≤ 70 mAh / 10 h`. | Open (awaiting empirical validation) |

Finished plans are archived under [`temp/finished-plans/`](../temp/finished-plans/): `brand-rollout.md`, `desktop-multi-device-support.md`, `readme_changes_plan.md`.

## Protocol reference (living docs)

| Doc | Purpose |
|------|--------|
| [protocol.md](protocol/protocol.md) | Formal spec of the HTTP + encrypted-envelope protocol between clients and relay. Reverse-specified from `main`. |
| [explain.protocol.md](protocol/explain.protocol.md) | Rationale for why `protocol.md` exists, how to extend it, how to use it during change design. |
| [protocol.compatibility.md](protocol.compatibility.md) | Per-row classification of every protocol surface change as preserving / extending / breaking. |
| [protocol.examples.md](protocol.examples.md) | Canonical request / response examples for each endpoint and mode. |
