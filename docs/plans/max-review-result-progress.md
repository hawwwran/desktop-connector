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

## High section — in progress

- 2026-05-17 — fixed §2.H1 (commit dbd11a9): hoist root-fetch floor check before the four cache writes so a rolled-back relay can't clobber the last-good cache.
- 2026-05-17 — fixed §3.H1 (commit e26cf1b): observe_with_detector records the event before forwarding; trip-causing event no longer leaks into the pending-ops queue.
- 2026-05-17 — fixed §1.H2 (commit 88802d3): getChunk applies isUserVisibleChunkState filter; GC-window race window closed.
- 2026-05-17 — fixed §3.H2 (commit 7bf3df4): download_folder threads should_continue; Cancel button bails within ~1 chunk of work.
- 2026-05-17 — fixed §3.H3 (commit 11f0e69): single-file downloads stream via atomic_write_chunks; peak RAM drops to ~1 chunk.
- 2026-05-17 — fixed §3.H4 (commit c98e913): two-way tombstone declines unlink fallback when gio is missing; op marks trash_failed instead of silent permanent delete.
- 2026-05-17 — fixed §3.H5 (commit 095523b): WatcherCoordinator now locks _pending/debouncer/gate across observer + tick threads.
- 2026-05-17 — fixed §6.H4 (commit 45abecc): passphrase generator uses PasswordEntry + 30 s clipboard auto-clear + clipboard-manager tip.
- 2026-05-17 — fixed §1.H1 (commit c041b46): vault auth + create rate limits per protocol §10; new vault_auth_attempts table + repo; 429 vault_rate_limited on overflow.
- 2026-05-17 — fixed §5.H1 (commit d6a2b11): export bundle persists genesis_fingerprint; Vault.fetch_header_plaintext + import wizard extract both sides; identity gate now anchored on the cryptographic fingerprint.
- 2026-05-17 — partial §6.H1 (commit 0b836aa): autosync notifies on due scheduled-purges + dialog copy is now honest; auto-fire half needs purge_secret persistence design (logged to review-doubts.md).
- 2026-05-17 — fixed §1.H3 (commit 08bbcd9): migrationStart now validates target_relay_url via shared helper with migrationCommit.
- 2026-05-17 — fixed §1.H4 (commit d5222cd): createVault parses both envelopes + guardFormatVersion + asserts envelope (vault_id, revision) matches body.
- 2026-05-17 — fixed §1.H5 (commit a28f46d): gcCancel sync-role caller must also be plan owner OR admin.
- 2026-05-17 — fixed §1.H6 (commit 380dc2b): chunk-write rollback pair wrapped in BEGIN IMMEDIATE/COMMIT.
- 2026-05-17 — fixed §2.H2 (commit e1fe2f9): batch CAS retry flips to use_merge=True after first 409 (mirrors folder-upload fix); §1.H4 cascade-fixed three test envelopes.
- 2026-05-17 — fixed §2.H3 (commit b1d2cde): FRESH_UNLOCK_WINDOW_S aligned with spec at 900 s (15 min).
- 2026-05-17 — fixed §3.H8 (commit f92b664): SQLite WAL + synchronous=NORMAL + busy_timeout=5000 on both local_index + bindings _connect.
- 2026-05-17 — fixed §3.H9 (commit 7b276bd): ghost-reaper refuses to demote when shard plaintext missing schema header; prevents self-DDoS.
