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
- 2026-05-17 — fixed §3.H7 (commit 7838450): _unique_conflict_path atomically reserves via O_CREAT|O_EXCL; TOCTOU between exists() and shutil.move closed.
- 2026-05-17 — fixed §3.H6 (commit 638cf3d): VaultRuntime.run_initial_baseline refuses when fresh head ≠ preflight head; dialog re-prompts user.
- 2026-05-17 — fixed §4.H5 (commit 64a3e78): upload resume seeks per-record plaintext_size, not session.chunk_size — last-chunk recovery on non-multiple files works.
- 2026-05-17 — fixed §4.H4 (commit caa220e): eviction cleanup-only stage 1 emits cascade-to-force warning + diagnostics catalog entry added.
- 2026-05-17 — fixed §4.H3 (commit 25470a3): clear_vault loops until root is stable; 8-pass cap + clear_pass_cap_hit warning catches abuse.
- 2026-05-17 — fixed §4.H2 (commit a0859e3): resume _probe_relay_state decrypts the header and asserts genesis_fingerprint; new VaultIdentityMismatchError catches cross-relay vault_id collisions before adopt.
- 2026-05-17 — fixed §4.H1 (commit 9aecb6d): upload pipeline unlinks session first + tombstone fallback; new reap_expired_sessions sweeps stale top-level JSON at vault open (14-day TTL).

§4 Highs complete (5 of 5: H1, H2, H3, H4, H5).

- 2026-05-17 — fixed §5.H4 (commit cbfe33f): open_bundle_for_preview gains optional relay kwarg + does batch_head_chunks inline; wizard threads the live relay so the preview's "chunks already on relay" count is real before commit.
- 2026-05-17 — skipped §5.H2 + §5.H3 (logged to review-doubts.md): per-folder conflict resolution UI + access-secret rotation client trigger are new feature builds.

§5 Highs: §5.H1 (d6a2b11), §5.H4 (cbfe33f) fixed; §5.H2 + §5.H3 logged as new-feature builds in review-doubts.md.

- 2026-05-17 — fixed §6.H5 (commit cd76131): tab_danger destructive callsites pass on_cancel to fresh-unlock prompt; explicit cancel feedback in status label.
- 2026-05-17 — fixed §6.H3 (commit b3d84ad): tray Sync-now does a real autosync kick; Export entry removed pending wizard build (logged to review-doubts.md).
- 2026-05-17 — skipped §6.H2 (logged to review-doubts.md): Revoke-device UI is a new feature build (Devices tab placeholder + locked §3.3 wording + double-gate).

§6 Highs: §6.H4 (45abecc), §6.H5 (cd76131), §6.H3 (b3d84ad) fixed; §6.H1 partial (0b836aa + review-doubts.md); §6.H2 logged as new-feature build.

- 2026-05-17 — fixed §7.H1 (commit 421f5be): cross-vault chunk replay tests pin AAD-vault_id + master_key separation; encrypt(A) fails to decrypt under (B-key, A-AAD) and (A-key, B-AAD).
- 2026-05-17 — fixed §7.H2 (commit 421f5be): content_fingerprint negative vectors — different-master-key + different-plaintext both pin inequality via expected.diverges_from_b64 across Python + PHP runners.
- 2026-05-17 — fixed §7.H3 (commit 421f5be): open_bundle_for_preview verified to make zero relay writes — every FakeUploadRelay write counter is unchanged after preview; only batch_head_calls increments.
- 2026-05-17 — fixed §7.H4 (commit 421f5be): export bundle reorder test — swap two on-disk chunk records, assert read raises vault_export_tampered (spec §16 record_index AAD + footer hash chain).
- 2026-05-17 — fixed §7.H5 (commit 421f5be): PHP getHeader-after-commit test drives /start → /verify → /commit and asserts migrated_to surfaces on GET /header (the discovery signal propagate_relay_migration consumes).

§7 Highs complete (5 of 5: H1, H2, H3, H4, H5).

## Milestone: All actionable Highs landed

§1.H1-H6, §2.H1-H3, §3.H1-H9, §4.H1-H5, §6.H3-H5, §7.H1-H5 all fixed.
Remaining open Highs are all *skipped (new feature build)* with explicit scoping in `review-doubts.md`:
- §5.H2 per-folder import conflict resolution UI
- §5.H3 access-secret rotation client trigger
- §6.H2 Revoke-device UI (Devices tab placeholder)
§6.H1 has a partial fix (notification on due purges) with the auto-execute design tracked in `review-doubts.md`.

Continuing into the Medium section.

## Medium section — in progress

- 2026-05-17 — fixed §1.M1 (commit 15ee4ca): vaultRequireHex64 validates header_hash/root_hash/shard_hash as ^[a-f0-9]{64}$ across all 6 write sites; 400 vault_invalid_request with field attribution.
- 2026-05-17 — fixed §1.M2 (commit 2e466fe): all 6 AAD builders now assert canonical 12-byte vault_id; the four previously-unguarded ones (chunk, header, recovery, device_grant) now match buildRootAad / buildShardAad.
- 2026-05-17 — fixed §1.M3 (commit 10fce54): gcExecute requireRole(sync) hoisted before plan lookup; read-only callers get uniform 403 without leaking plan state.
- 2026-05-17 — fixed §1.M4 (commit 2ccafd4): Router catches \Throwable and emits typed 500 vault_internal_error; full trace logged via new apierror.uncaught_throwable event.
- 2026-05-17 — fixed §1.M5 (commit d91ea3a): rotateAccessSecret wrapped in BEGIN IMMEDIATE/COMMIT; rotation+audit either both land or neither does; missing vault row → 404.
- 2026-05-17 — fixed §1.M6 (commit 6e5df0c): decodeBase64Field explicit strlen($raw)===0 guard in both controllers (defense-in-depth; strict-mode already covers practical attack).
- 2026-05-17 — fixed §1.M7 (commit 86a7087): migrationVerifySource explicit migrated_to state guard so post-commit /verify never stamps a fresh verified_at.

§1 Mediums complete (7 of 7: M1-M7).

- 2026-05-17 — fixed §2.M4 (commit afe28db): browser_model.decrypt_manifest split into root-only + decrypt_bundle_manifest_envelope (legacy bundle path); legacy HKDF label is now reachable only via the deliberately-named bundle helper.
- 2026-05-17 — fixed §2.M1 (commit 4b4901d): decrypt_root_envelope + decrypt_shard_envelope add explicit envelope-prefix vault_id check mirroring fetch_header_plaintext.
- 2026-05-17 — fixed §2.M2 (commit 0e8a619): four production decrypt sites now call assert_supported_format_version → typed VaultFormatVersionUnsupported with structured envelope_kind + observed_version.
- 2026-05-17 — fixed §2.M5 (commit 91a278c): nfcNormalize hard-fails when non-ASCII input + intl missing; ASCII passthrough preserved.
- 2026-05-17 — fixed §2.M3 (commit d159811): added parse_recovery_envelope + RECOVERY_ENVELOPE_TOTAL_LEN so the §12.4 wire form is round-trippable in code (production unwrap migration is a follow-up).

§2 Mediums complete (5 of 5: M1, M2, M3, M4, M5).

- 2026-05-17 — fixed §3.M1 (commit 46c36b5): run_initial_baseline refuses on non-preflight binding so a second call can't clobber two-way local_entries.
- 2026-05-17 — fixed §3.M6 (commit 6359f3d): download_folder fsyncs every distinct destination directory after per-file loop so power loss doesn't lose entries.
- 2026-05-17 — fixed §3.M2 (commit 2d22e81): _apply_remote_upsert catches _unique_conflict_path's RuntimeError as typed failed outcome; cycle continues with other ops.
- 2026-05-17 — fixed §3.M3 (commit 6ffeb67): stability-gate timeout surfaces user-visible signal via runtime-level set_stability_timeout_callback; log severity bumped to error.
- 2026-05-17 — fixed §3.M4 (commit 13f63bf): docs clarification — prep-time reads happen AFTER stability gate settles; last-write-wins is correct by design, not a race.
- 2026-05-17 — fixed §3.M5 (commit 117c7b5): disconnect_binding force=False refuses pending-ops drop (typed VaultDisconnectHasPendingOpsError); UI gets count + re-call seam.

§3 Mediums complete (6 of 6: M1, M2, M3, M4, M5, M6).

- 2026-05-17 — fixed §4.M3 (commit 19884ad): clear_vault emits vault.vault.clear_started at top of loop; paired with terminal cleared so mid-loop crash has audit trail.
- 2026-05-17 — fixed §4.M4 (commit 19884ad): schedule_purge enforces MIN_DELAY_SECONDS=60 floor with "cancel window" remediation message; UI restricts dropdown ≥1h separately.
- 2026-05-17 — fixed §4.M2 (commit 557de31): restore_remote_folder symlink-escape check hoisted above mkdir; no side-effect directories created on the wrong side before skip.
- 2026-05-17 — skipped §4.M1 (logged to review-doubts.md): orphan-chunk reaper needs new GET /chunks endpoint or server-side KIND_RECLAIM_ORPHAN_CHUNKS GC job; leak is bounded by 30-day retention.

§4 Mediums: 3 fixed (M2, M3, M4), 1 logged as needs-design (M1).

- 2026-05-17 — fixed §5.M5 (commit bfc1792): write_export_bundle refuses passphrases below EXPORT_PASSPHRASE_MIN_LEN=8.
- 2026-05-17 — fixed §5.M1 (commit bfc1792): import wizard wipes state[passphrase] + entry buffer on every terminal path (cancel/cancelled/fail/succeed).
- 2026-05-17 — fixed §5.M4 (commit 388f85f): documented brute-force decrypt cost + format-bump migration path (constant-time AEAD makes it safe today).
- 2026-05-17 — skipped §5.M2 (logged): bundle with §5.C1 migration wizard.
- 2026-05-17 — skipped §5.M3 (logged): per-subprocess fresh-unlock is more secure than spec; three resolution paths documented.
- 2026-05-17 — skipped §5.M6 (logged): bundle with §5.C1 migration wizard UX.

§5 Mediums: 3 fixed (M1, M4, M5), 3 logged as conditional/needs-design (M2, M3, M6).

- 2026-05-17 — fixed §6.M1 + §6.M2 (commit c9afb0d): disconnect-vault dialog gains warning glyph + bulleted artefact list; add-folder library-layer name-collision check (case-insensitive + trim).

§6 Mediums complete (2 of 2).

- 2026-05-17 — fixed §7.M5 (commit df9f976): chunk-index AAD flip vector + frozenset pin; cross-runtime parity verified.
- 2026-05-17 — fixed §7.M4 (commit df9f976): quota 507 byte-precise pins via assertSame(false, ...) + assertArrayHasKey for documented keys.
- 2026-05-17 — fixed §7.M3 (commit df9f976): server-clock authority source-grep pins all controllers; no client-supplied timestamps reach DB writes.
- 2026-05-17 — fixed §7.M2 (commit df9f976): folder-runtime serialization test uses poll-on-state instead of sleep(50ms); CI-resilient.
- 2026-05-17 — fixed §7.M1 (commit df9f976): production Argon2id (128 MiB / 4 iter) end-to-end test with byte-exact output (~170ms).

§7 Mediums complete (5 of 5).

## Milestone: All Mediums processed

§1.M1-M7 + §2.M1-M5 + §3.M1-M6 + §4.M2-M4 + §5.M1+M4+M5 + §6.M1+M2 + §7.M1-M5 all fixed.
Logged as needs-design / conditional:
- §4.M1 (orphan-chunk reaper needs server-side support)
- §5.M2 + §5.M6 (bundle with §5.C1 migration wizard)
- §5.M3 (cross-subprocess fresh-unlock — security/UX tradeoff documented)

Continuing into the Low / Info section will be lighter — most §1.L–§7.L items are already noted as "acceptable for v1" in the review.
