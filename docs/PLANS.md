# Plans Index

Working notes under [`plans/`](./plans/) (gitignored, local only).
Each file is a self-contained plan; some have been acted on, most
haven't yet.

## Status legend
- **Done** — landed on `main`, plan file carries a completion header
- **Draft** — plan is written and reviewed, implementation not yet started
- **Deferred** — conditionally useful, waiting on a measurement or
  another plan to land first

---

## Desktop client

| Plan | Intent | Status |
|------|--------|--------|
| [desktop-client-migration-plan.md](plans/desktop-client-migration-plan.md) | Migrate desktop off pystray + GTK-subprocess to PySide6 (pragmatic path) or Rust core + Qt shell (long term). | Draft |
| [hardening-plan.md](plans/hardening-plan.md) | Improve at-rest secret storage on the desktop — `auth_token`, paired-device symmetric keys, private key currently sit in `~/.config/desktop-connector/`. | Draft |
| [secrets-and-signing-plan.md](plans/secrets-and-signing-plan.md) | Move Android signing passwords out of `build.gradle.kts`; define keystore backup, machine-migration, and server Firebase service-account hygiene. | Draft |
| [appimage-distro-support-plan.md](plans/appimage-distro-support-plan.md) | Ship the desktop client as an AppImage per architecture, define a realistic tested-distro list vs. expected-to-work vs. unsupported. | Draft |

## Brand / visual identity

| Plan | Intent | Status |
|------|--------|--------|
| [brand-rollout.md](plans/brand-rollout.md) | Apply `visual-identity-guide.md` across all three components. | Android **Done** (v0.2.0) · Desktop Draft · Server Draft |

## Protocol & docs

| Plan | Intent | Status |
|------|--------|--------|
| [protocol.md](plans/protocol.md) | Formal spec of the HTTP + encrypted-envelope protocol between desktop, phone, relay. Reverse-specified from `main`. | Reference (living doc) |
| [explain.protocol.md](plans/explain.protocol.md) | Rationale for why `protocol.md` exists, how to extend it, how to use it during change design. | Reference |
| [readme_changes_plan.md](plans/readme_changes_plan.md) | Sharpen the top-level README so the project presents more credibly to first-time visitors and contributors. | Draft |
