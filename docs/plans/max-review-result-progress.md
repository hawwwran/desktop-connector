# Vault v1 max-effort review — fix progress log

Append-only log of fixes landing against `max-review-result.md`. One line per fix.

- 2026-05-17 — fixed §1.C1 (commit 5d3efbb): putChunk re-charges quota and revives a purged chunk row instead of returning 200 with no blob.
- 2026-05-17 — fixed §1.C2 (commit 639d22b): gcExecute residual-purged reaper retries unlinks and deletes the orphan row on success; post-commit loop also drops the row on successful unlink.
- 2026-05-17 — fixed §1.C3 (commit eeee9c3): same-device /migration/start retry rotates token-hash + emits a fresh bearer; cross-target still 409; cross-device still metadata-only.
- 2026-05-17 — partial §3.C1 (commit f621dc1): eviction stages 2/3 now require admin role via new KIND_FORCED_EVICTION; purge_secret + passphrase UI logged to review-doubts.md.
- 2026-05-17 — fixed §3.C2 (commit 3988c06): ransomware-detector trip threads BindingCancellationRegistry into pause_binding; shared registry wired through tray autosync.
- 2026-05-17 — fixed §3.C3 (commit 92c5fed): scan + preflight lstat leaves + S_ISREG gate; symlinks skipped with special_file_skipped breadcrumb.
- 2026-05-17 — skipped §5.C1 + §5.C2 (logged to review-doubts.md): migration wizard + QR-join UI are new feature builds; recovery-kit path is the v1 multi-device story per memory.
- 2026-05-17 — fixed §5.C3 (commit 2627d9a): VaultHttpRelay.get_header now applies propagate_relay_migration and persists config switch on migrated_to.
- 2026-05-17 — fixed §6.C1 + §6.C2 + §6.C3 (commit c080d99): three GTK click handlers now off-load Argon2id and the relay POST to threading.Thread workers; settle via GLib.idle_add.
- 2026-05-17 — fixed §6.C4 (commit 800de43): browser "Delete folder contents" gated on fresh-unlock + typed-confirm matching the Danger tab flow.
- 2026-05-17 — fixed §6.C5 (commit 6f85a0d): GET /header returns caller_role; tab_danger.on_schedule_purge async-checks role and refuses non-admin devices.
- 2026-05-17 — fixed §7.C1 + §7.C2 (commit 40e03c2): PHP twin gains rootCases + shardCases (12 vectors); controller-level 422 format-version gate has integration tests across all four PUT endpoints.
- 2026-05-17 — fixed §7.C3 (commit fa813a7): ConflictInjectingRelay drives a true §D4 merge-retry-merge cycle with 4 sequential CAS conflicts; asserts B terminates, head convergence, no entries lost.

## Milestone: Criticals done

All 17 Criticals processed on 2026-05-17.

- **Fully fixed (13):** §1.C1, §1.C2, §1.C3, §3.C2, §3.C3, §5.C3, §6.C1, §6.C2, §6.C3, §6.C4, §6.C5, §7.C1, §7.C2 (and §7.C3 — test gap closure).
- **Partial fix (1):** §3.C1 — admin gate landed (new KIND_FORCED_EVICTION), purge_secret + passphrase UI still open in `review-doubts.md`.
- **Skipped (logged to doubts) (2):** §5.C1 migration wizard UI, §5.C2 QR-join UI — both are new feature builds, never autonomous; memory note `project_vault_multi_device_story.md` confirms recovery-kit is the v1 multi-device story.
- **One-line per doubt:** see `docs/plans/review-doubts.md` for §3.C1 (purge_secret follow-up), §5.C1 (migration wizard scoping), §5.C2 (QR-join wait-for-v1.1 confirmation).

Continuing autonomously into the High section.
