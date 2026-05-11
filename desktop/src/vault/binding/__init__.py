"""Binding subsystem: per-folder local↔remote sync state machine.

Submodules:
- ``bindings`` — SQLite store schema + ``VaultBinding`` / ``VaultBindingsStore``
- ``baseline`` — initial baseline scan for a fresh binding
- ``lifecycle`` — disconnect / pause / resume + ``SyncCancelledError``
- ``preflight`` — pre-connect summary (count, bytes, tombstones, writability)
- ``scan`` — local filesystem walk + ignore-pattern matching
- ``sync`` — backup-only sync cycle (push local changes only)
- ``twoway`` — two-way sync engine (push + pull, conflict-rename)
- ``filesystem_watcher`` — watchdog observer + event coalescer
- ``runtime`` — relay adapter (``VaultHttpRelay``) + grant load/save helpers
- ``runtime_watchers`` — top-level watcher lifecycle driven by ``VaultRuntime``
"""
