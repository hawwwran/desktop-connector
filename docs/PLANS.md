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

## Android client

| Plan | Intent | Status |
|------|--------|--------|
| [android-streaming-upload-plan.md](plans/android-streaming-upload-plan.md) | Stream every outgoing file chunk-by-chunk (no whole-file buffering), add `PREPARING` state, 5 s / 120 s per-chunk retry. | **Done** |
| [android-streaming-receive-plan.md](plans/android-streaming-receive-plan.md) | Stream incoming file chunks directly to a temp file, atomic-rename on finalize, keep `.fn.*` on the tiny in-memory path. | **Done** |
| [android-pipelining-plan.md](plans/android-pipelining-plan.md) | Overlap chunk read/encrypt with upload (and download with decrypt/write) via a bounded `Channel`. Explicitly rejects request-level parallelism. | **Deferred** — only build once a 500 MB benchmark shows CPU, not network, is the bottleneck |

## Desktop client

| Plan | Intent | Status |
|------|--------|--------|
| [desktop-client-migration-plan.md](plans/desktop-client-migration-plan.md) | Migrate desktop off pystray + GTK-subprocess to PySide6 (pragmatic path) or Rust core + Qt shell (long term). | Draft |
| [hardening-plan.md](plans/hardening-plan.md) | Improve at-rest secret storage on the desktop — `auth_token`, paired-device symmetric keys, private key currently sit in `~/.config/desktop-connector/`. | Draft |
| [appimage-distro-support-plan.md](plans/appimage-distro-support-plan.md) | Ship the desktop client as an AppImage per architecture, define a realistic tested-distro list vs. expected-to-work vs. unsupported. | Draft |

## Protocol & docs

| Plan | Intent | Status |
|------|--------|--------|
| [protocol.md](plans/protocol.md) | Formal spec of the HTTP + encrypted-envelope protocol between desktop, phone, relay. Reverse-specified from `main`. | Reference (living doc) |
| [explain.protocol.md](plans/explain.protocol.md) | Rationale for why `protocol.md` exists, how to extend it, how to use it during change design. | Reference |
| [readme_changes_plan.md](plans/readme_changes_plan.md) | Sharpen the top-level README so the project presents more credibly to first-time visitors and contributors. | Draft |

## Server / structural refactor sequence

A 10-step ordered refactor plan. The overview lives in
[refactor.md](plans/refactor.md); each step is its own file. Order matters —
later steps assume earlier ones.

| # | Plan | Intent | Status |
|---|------|--------|--------|
| 0 | [refactor.md](plans/refactor.md) | Overview of the 10-step sequence and the reasoning behind its order. | Reference |
| 10 | [refactor-10.md](plans/refactor-10.md) | Prepare for a Windows desktop client via a platform-abstraction boundary (last, deliberately). | Draft |
