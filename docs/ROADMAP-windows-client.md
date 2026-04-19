# Windows Client Gap Map (Post Refactor #10)

This document tracks what is *architecturally ready* versus what is still
missing before a real Windows desktop client implementation begins.

## Scope and intent

Refactor #10 does **not** implement Windows behavior. It prepares a stable
platform boundary so Windows work can be attached without reopening core
architecture.

## Architecture-ready areas

These areas now have a defined place in the desktop runtime model:

- First-class desktop platform contract (`DesktopPlatform`)
- Explicit platform capabilities (`PlatformCapabilities`)
- Explicit Linux platform implementation (`LinuxDesktopPlatform`)
- Centralized platform composition (`platform/compose.py`)
- Core runtime modules (`startup_context`, `receiver_runner`, `poller`, `tray`)
  consume the platform contract instead of Linux backend bundles.

## Partially-ready areas

These areas are represented, but still Linux-only in implementation:

- Clipboard behavior
- Notifications
- Dialog flow
- Shell/open behavior
- Tray runtime assumptions
- File-manager integration assumptions

The contract exists; Windows implementations for these capabilities do not.

## Unresolved implementation areas for Windows

Concrete gaps before Windows can ship:

1. Windows backend implementations for clipboard/notifications/dialogs/shell.
2. Windows tray runtime behavior and lifecycle model.
3. Windows file-manager integration mechanism (e.g., Send To, shell extension).
4. Windows installer/update path and bootstrap dependency story.
5. Windows-specific dependency checks and install UX.
6. Platform-aware packaging/distribution and signing decisions.

## Likely difficult items

These are expected to require design iteration:

- Tray lifecycle differences and background/runtime expectations on Windows.
- Notification behavior parity and failure semantics.
- Installer UX + update strategy that does not regress Linux behavior.
- Packaging/signing tradeoffs for user trust and onboarding.

## Non-goals for first Windows version

- Cross-platform CI matrix completeness on day one.
- Perfect parity for every Linux file-manager integration behavior.
- Full installer auto-update sophistication in the first release.

## Validation expectations before implementation starts

- Linux behavior remains unchanged for pairing, transfer, tray receive,
  headless receive, and clipboard flows.
- Platform capability checks are the default branching mechanism in core.
- New Windows code lands as a platform implementation, not as core branching.
