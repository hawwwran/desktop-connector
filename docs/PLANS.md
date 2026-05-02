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
| [brand-rollout.md](plans/brand-rollout.md) | Apply `visual-identity-guide.md` across Android, desktop, and server. | Done |
| [desktop-multi-device-support.md](plans/desktop-multi-device-support.md) | Add desktop support for multiple paired connected devices, target pickers, per-device file-manager actions, and find-device receiver behavior. | Done; manual GTK smoke / lock-screen hardening remain documented follow-ups |
| [readme_changes_plan.md](plans/readme_changes_plan.md) | Sharpen the top-level README so the project presents more credibly to first-time visitors and contributors. | Done |
| [desktop-connector-tresor-plan-md/](plans/desktop-connector-tresor-plan-md/desktop-connector-tresor-00-index.md) | 11-part incremental plan to add a persistent, account-less, E2E-encrypted **Tresor / Vault** with remote folders, browse-only import, versioned upload/delete, export/import bundles, and guarded sync engine. | Open |
| [desktop-connector-vault-open-ux-gaps.md](plans/desktop-connector-vault-open-ux-gaps.md) | Discussion notes supplementing the Tresor plan: recovery testing, emergency access state, and other UX/safety gaps to resolve before or during Vault implementation. | Open |

## Protocol reference (living docs)

| Doc | Purpose |
|------|--------|
| [protocol.md](protocol/protocol.md) | Formal spec of the HTTP + encrypted-envelope protocol between clients and relay. Reverse-specified from `main`. |
| [explain.protocol.md](protocol/explain.protocol.md) | Rationale for why `protocol.md` exists, how to extend it, how to use it during change design. |
| [protocol.compatibility.md](protocol.compatibility.md) | Per-row classification of every protocol surface change as preserving / extending / breaking. |
| [protocol.examples.md](protocol.examples.md) | Canonical request / response examples for each endpoint and mode. |
