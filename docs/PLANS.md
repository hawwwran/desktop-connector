# Plans Index

Working notes under [`plans/`](./plans/) (tracked in git; small,
current set). Protocol reference docs under
[`protocol/`](./protocol/). Release runbooks under
[`release/`](./release/).

## Status legend
- **Open** — implementation not started, or in progress
- **Reference** — living spec, never "done"; updated as the
  surface changes

---

## Open plans

| Plan | Intent | Status |
|------|--------|--------|
| [brand-rollout.md](plans/brand-rollout.md) | Apply `visual-identity-guide.md` across desktop and server. | Android **Done** (v0.2.0) · Desktop Open · Server Open |
| [desktop-multi-device-support.md](plans/desktop-multi-device-support.md) | Add desktop support for multiple paired connected devices, target pickers, per-device file-manager actions, and find-device receiver behavior. | Open |
| [readme_changes_plan.md](plans/readme_changes_plan.md) | Sharpen the top-level README so the project presents more credibly to first-time visitors and contributors. | Open |

## Protocol reference (living docs)

| Doc | Purpose |
|------|--------|
| [protocol.md](protocol/protocol.md) | Formal spec of the HTTP + encrypted-envelope protocol between desktop, phone, relay. Reverse-specified from `main`. |
| [explain.protocol.md](protocol/explain.protocol.md) | Rationale for why `protocol.md` exists, how to extend it, how to use it during change design. |
| [protocol.compatibility.md](protocol.compatibility.md) | Per-row classification of every protocol surface change as preserving / extending / breaking. |
| [protocol.examples.md](protocol.examples.md) | Canonical request / response examples for each endpoint and mode. |
