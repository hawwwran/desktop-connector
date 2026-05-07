# Desktop file-size audit and breakup plan

**Date:** 2026-05-07
**Scope:** every source file under `desktop/` (Python, shell, config, docs).
**Goal:** identify the longest modules and propose concrete, small-step refactors that split them into cohesive units without changing behavior.

## Why this matters

Yes — the user is right. Several modules in `desktop/src/` are well past the size where a human can hold the whole file in their head, and they keep accreting because:

- Adding a new dialog "right next to the existing one" is faster than carving a module.
- GTK4 windows in particular nest dozens of inner closures inside one giant `on_activate`.
- Vault has grown from one feature into a subsystem, but the module names still read like a flat namespace.

Concrete pain caused by 1500–2500-line files:

- **Review.** Diffs land in a wall-of-context that's hard to skim.
- **Test.** Closures inside closures are unreachable from unit tests.
- **Concurrency reasoning.** When all GTK callbacks live in one `def show_*`, you can't see at a glance which thread mutates which captured variable.
- **Merge conflicts.** A 1000-line PR touching `windows_vault.py` collides with anything else in the same window.
- **AI assistance.** Tools (Claude included) work better on files that fit cleanly in a single read; large files get truncated or forced into multiple passes.

Healthy targets, applied loosely:

- Python modules: aim for **≤ 500 lines**, hard ceiling around **800**.
- GTK windows: one `Gtk.ApplicationWindow` per file, helpers and tabs in siblings.
- Top-level functions > 200 lines almost always want to be a class with methods.

## Refactor principles (apply to all 10)

1. **Move first, refactor second.** Each split should be a pure relocation: same names, same signatures, just imported from a new module. Behavior changes belong in a separate commit.
2. **Folderize when ≥ 3 siblings exist.** Once you have `vault_*` files numbering in the dozens, they belong under `vault/`. Same for `windows_*`, `bootstrap/` (already done), `runners/` (already done).
3. **Keep public re-exports for one release.** A leftover `vault.py` that re-exports `Vault`, `RelayProtocol`, etc. from `vault/core.py` keeps callers unaffected during the transition.
4. **Each split needs a test.** If it's untestable post-split, the split is wrong (you peeled state away from behavior).
5. **No dead `_legacy` files.** Either delete or finish the move; do not leave half-renamed siblings.

## Top 10 — breakup plans

### 1. `desktop/src/windows_vault.py` — 107 164 B / 2467 lines

Three independent GTK4 entry points crammed into one file:
`show_vault_main`, `show_vault_onboard`, `show_vault_passphrase_generator`. `show_vault_main` alone is ~1500 lines with nested tabs (Recovery, Activity, Maintenance, Migration, Danger).

**Split into a package `desktop/src/windows_vault/`:**

```
windows_vault/
  __init__.py                # re-export show_vault_main / show_vault_onboard / show_vault_passphrase_generator
  main_window.py             # show_vault_main shell + sidebar/stack/header
  tab_recovery.py            # recovery summary, "Test recovery" dialog (lines ~291–582)
  tab_activity.py            # activity log render + filter + refresh (lines ~582–725)
  tab_maintenance.py         # debug bundle, integrity check (lines ~726–987)
  tab_migration.py           # switch-back / disconnect (lines ~988–1145)
  tab_danger.py              # clear folder, clear vault, schedule purge (lines ~1145–1588)
  onboard_window.py          # show_vault_onboard
  passphrase_generator.py    # show_vault_passphrase_generator
  _kv_row.py                 # the single layout helper at module top
```

Each tab module exports a single `build_<tab>(ctx) -> Gtk.Widget`. `main_window.py` becomes a thin composer.

### 2. `desktop/src/windows_vault_browser.py` — 82 555 B / 1891 lines

One giant `show_vault_browser` (line 35 → 1872) with everything inline: tree, file list, detail pane, version section, navigation, upload, async manifest refresh.

**Split into `desktop/src/windows_vault_browser/`:**

```
windows_vault_browser/
  __init__.py                # re-export show_vault_browser
  app.py                     # window shell, on_activate, layout
  tree_pane.py               # render_tree, walk, add_path_button
  file_list.py               # render_file_list, attach_cell/label
  detail_pane.py             # render_detail, render_versions_section
  navigation.py              # navigate_to, go_back, go_forward, history stack
  manifest_refresh.py        # async manifest fetch + state machine
  upload_action.py           # _resolve_upload_destination + upload trigger
  cancel_progress.py         # _arm_cancel, _show_progress_no_cancel, _disarm_cancel
  formatting.py              # _format_bytes, _download_folder_name (already at module bottom)
```

This is the biggest win for testability — the navigation stack and manifest state can be unit-tested once they're not inside `on_activate`.

### 3. `desktop/src/poller.py` — 72 113 B / 1641 lines

`Poller` is the receive-side state machine: long-poll, classic+streaming downloads, fasttrack consumer, fn-transfer adapter, delivery tracker. ~30 methods, multiple distinct responsibilities.

**Split `Poller` into a small package `desktop/src/receive/`:**

```
receive/
  __init__.py                # re-export Poller (composes the helpers below)
  poller.py                  # Poller.run + lifecycle (init, stop, wake, on_file_received)
  long_poll.py               # _long_poll, _test_long_poll, retry_long_poll
  delivery_tracker.py        # _delivery_tracker_loop, _check_delivery_status, _process_delivery_*
  classic_download.py        # _receive_file_transfer, _download_and_decrypt_chunk, _sweep_stale_parts
  streaming_download.py      # _receive_streaming_transfer, _stream_download_chunk
  finalize.py                # _finalize_temp_to_unique, _fallback_move_unique, _delete_quietly
  fn_transfer.py             # _receive_fn_transfer, _handle_fn_transfer, _handle_message_*
  fasttrack.py               # _fasttrack_consumer_loop, _process_fasttrack_pending, _dispatch_fasttrack_message
  receive_actions.py         # _apply_receive_file_action, flood-summary, _notify_receive_action_flood_summary
  device_helpers.py          # _mark_active_device, _lookup_device_name, _resolve_symmetric_key
  clipboard_image.py         # _clipboard_image_extension, _clipboard_image_filename (already module-level)
```

Each module receives the `Poller` `self` (or a small `ReceiveContext` value) and exposes pure functions. Keeps the public Poller surface unchanged.

### 4. `desktop/src/vault_upload.py` — 57 982 B / 1593 lines

Already pretty well structured (clear dataclasses + `upload_file` / `resume_upload` / `upload_folder` + private helpers), but it covers four distinct concerns.

**Split into `desktop/src/vault/upload/`:**

```
vault/upload/
  __init__.py                # re-export public API
  protocols.py               # UploadVault, UploadRelay (Protocol classes)
  results.py                 # FileSkipped, FolderUploadResult, UploadProgress, UploadResult, FolderUploadProgress
  errors.py                  # UploadConflictError, UploadSpecialFileSkipped, UploadFileTooLargeError, describe_quota_exceeded
  session.py                 # UploadSession, default_upload_resume_dir, save_session, clear_session, list_resumable_sessions
  single_file.py             # upload_file + _build_chunk_plan + _make_version_payload + _publish_with_cas_retry
  resume.py                  # resume_upload
  folder.py                  # upload_folder, _walk_for_upload, _upload_one_into_batch, _publish_batch_with_cas_retry, _report_folder
  ignore_patterns.py         # _matches_ignore, _warn_unsupported_pattern
  conflict.py                # make_conflict_renamed_path, detect_path_conflict
  hashing.py                 # _hash_file, _now_rfc3339
```

### 5. `desktop/src/tray.py` — 54 932 B / 1280 lines

`TrayApp` is a 1100-line god class: icon compositing, menu building, update checker, vault submenu, ping logic, send-clipboard, repair flow.

**Split into `desktop/src/tray/`:**

```
tray/
  __init__.py                # re-export TrayApp
  app.py                     # TrayApp class skeleton + lifecycle (run/stop/icon_poll)
  icon_assets.py             # _load_master, _tint, _crop_and_pad, _load_icons, _make_icon, _bake_state_paths
  status.py                  # _current_state_key, _update_icon, _status_text, _auth_banner_text
  menu.py                    # build_menu (the giant inner def)
  ping.py                    # _maybe_ping
  open_window.py             # _open_gtk4_window, _send_files, _show_settings, _show_history, _find_phone, _pair, _open_folder
  send_clipboard.py          # _send_clipboard, _do_send_clipboard
  repair.py                  # _repair, _reregister_after_wipe, _show_secret_storage_warning
  update_check.py            # _has_pending_update, _update_check_loop, _refresh_update_info, _manual_update_check, _do_manual_check, _install_update, _do_install_update, _dismiss_update, _open_release_notes
  vault_submenu.py           # _vault_submenu_visible, _local_vault_exists, _build_vault_submenu, _vault_submenu_entry_visible, _spawn_vault_wizard
```

### 6. `desktop/src/vault_folders_tab.py` — 53 959 B / 1405 lines

A single 1300-line `build_vault_folders_tab` with 50 nested closures.

**Split into `desktop/src/vault_folders/`:**

```
vault_folders/
  __init__.py                # re-export build_vault_folders_tab
  tab.py                     # the composer: layout + sidebar + detail pane + state plumbing
  data.py                    # list_folders, list_bindings_for_folder, _lookup_folder_settings, refresh_folders_usage_async
  actions_sync.py            # run_sync_now, run_pause, run_resume, _idle_finish
  actions_disconnect.py      # run_disconnect (with confirmation dialog)
  dialog_add_folder.py       # open_add_folder_dialog
  dialog_configure_folder.py # open_configure_folder_dialog
  dialog_connect_local.py    # open_connect_local_dialog (+ baseline runner)
  dialog_response_details.py # _present_response_details_dialog
  rows.py                    # _build_binding_row, _build_sidebar_row, _build_empty_state, render_detail, refresh_sidebar
  buttons.py                 # _make_flat_action_button, _make_overflow_button
```

The new `tab.py` should pass a small `FoldersContext` dataclass into each helper instead of capturing a forest of locals.

### 7. `desktop/src/api_client.py` — 46 927 B / 1029 lines

`ApiClient` is the relay HTTP surface — registration, pairing, transfers, fasttrack, capabilities, ping. Cohesive purpose but split-able by route family.

**Split into `desktop/src/api/`:**

```
api/
  __init__.py                # re-export ApiClient (composes the mixins below)
  client.py                  # ApiClient base: __init__, connection, retry helpers, _parse_retry_after_ms, _extract_abort_reason
  outcomes.py                # ChunkUploadOutcome, ChunkDownloadOutcome, DeviceRegistrationResult
  registration.py            # register, register_with_status
  pairing.py                 # send_pairing_request, poll_pairing, confirm_pairing
  transfers_init.py          # init_transfer, _init_transfer_with_retry
  transfers_chunks.py        # upload_chunk, download_chunk, ack_transfer, ack_chunk, _upload_chunk_with_retry
  transfers_streaming.py     # _upload_stream, _upload_stream_chunk
  transfers_lifecycle.py     # abort_transfer, cancel_transfer, get_pending_transfers, get_sent_status
  transfers_send.py          # send_file (the high-level orchestrator)
  fasttrack.py               # fasttrack_send, fasttrack_pending, fasttrack_ack
  liveness.py                # ping_device, get_stats
  capabilities.py            # get_capabilities, supports_streaming, check_fcm_available
```

`ApiClient` becomes a thin composition of these route classes (mixin- or attribute-style — pick one, not both).

### 8. `desktop/src/windows_history.py` — 45 417 B / 998 lines

Single `show_history` with deeply nested per-row builders, abort flow, and zombie scrubber.

**Split into `desktop/src/windows_history/`:**

```
windows_history/
  __init__.py                # re-export show_history
  window.py                  # show_history shell + on_activate + window-level state
  zombie_scrub.py            # _scrub_zombie_waiting
  status.py                  # _compute_status, _row_key
  url_helpers.py             # _contains_single_url, _extract_single_url, on_item_click (URL-open flow)
  rows.py                    # _create_row, _update_row
  toast.py                   # show_toast
  clear_all.py               # on_clear_all + confirmation
  delete_row.py              # on_delete, _do_local_remove, abort dialog flow
  device_filter.py           # _selected_device_id, _selected_device_name, _empty_history_text, _reset_history_view, on_history_device_changed
  refresh.py                 # build_list, refresh_tick
```

### 9. `desktop/src/vault.py` — 45 231 B / 1161 lines

Decent existing structure (one `Vault` class + recovery-kit helpers at the bottom), but mixing recovery/RecoveryKit serialization with the core vault is the obvious cut.

**Split into `desktop/src/vault/core/`:**

```
vault/core/
  __init__.py                # re-export Vault, RelayProtocol, vault_id_dashed, recovery_kit_path, etc.
  protocols.py               # RelayProtocol
  vault.py                   # Vault class + create_new / prepare_new / open / from_grant / publish_initial / publish_manifest / fetch_manifest / decrypt_manifest / close
  remote_folders.py          # add_remote_folder, rename_remote_folder, update_remote_folder_settings (delegated from Vault if you prefer methods stay on the class — keep them as free functions taking Vault)
  ids.py                     # _generate_vault_id, _generate_id_v1, _genesis_fingerprint_hex, vault_id_dashed
  canonical.py               # _canonical_json, _now_rfc3339
recovery_kit.py              # write_recovery_kit_file, parse_recovery_kit_file, verify_recovery_kit, recovery_envelope_meta_to_json/_from_json, recovery_kit_path, shred_file
```

Note: `vault.py` (the file) should *become* the package `vault/` — and the giant flat namespace of `vault_*.py` files in `desktop/src/` should fold into `vault/` siblings (`vault/upload/`, `vault/manifest.py`, `vault/restore.py`, etc.). That folder reorganization is a separate, larger effort tracked in a sibling plan.

### 10. `desktop/src/windows_settings.py` — 37 915 B / 934 lines

Settings window with several independent groups: relay, theme, vault toggle, receive-action limits, logs, pairings, secret storage.

**Split into `desktop/src/windows_settings/`:**

```
windows_settings/
  __init__.py                # re-export show_settings
  window.py                  # show_settings shell, on_activate, on_save
  group_relay.py             # relay URL, on_retry_lp, refresh_lp_status
  group_theme.py             # theme combo + on_theme_changed
  group_vault.py             # vault_exists_locally, refresh_vault_button, on_vault_toggled, on_open_vault_clicked
  group_receive_actions.py   # on_receive_action_changed, make_limit_spin, on_limit_changed, on_reset_limits
  group_logs.py              # add_logs_group, on_download_logs, on_clear_logs
  group_pairings.py          # PairingsCard rows, open_rename_dialog, open_unpair_dialog, on_add_pair
  group_secret_storage.py    # _on_secret_info, _on_verify_secret_storage
```

Each `group_*.py` exposes a single `build(ctx) -> Gtk.Widget` and owns its own callbacks.

## Follow-up — emerged during the original 10

### 11. `desktop/src/windows_vault_browser/app.py` — ~2338 lines

Created during refactor #2 (the structural rewrite of the original
`windows_vault_browser.py` monolith into a `VaultBrowser` class
under `windows_vault_browser/`). The class itself is now coherent
— state is on `BrowserState`, no more captured closure spaghetti —
but the single file is still 2338 lines because every method
landed there. The class already has 9 clear section dividers
(grep `# -----` for them); each one is a candidate mixin module.

Same shape as `tray.py` (#5) used: per-topic mixins composed onto
the orchestrator class.

**Split into siblings under `windows_vault_browser/`:**

```
windows_vault_browser/
  __init__.py            # re-export show_vault_browser, VaultBrowser
  state.py               # BrowserState (already split)
  app.py                 # VaultBrowser orchestrator: __init__, run, _on_activate, _build_*
  status.py              # StatusMixin: _set_status, _current_path_label, _update_nav_buttons, _render_all
  tree_pane.py           # TreePaneMixin: _render_tree
  file_list.py           # FileListMixin: _attach_cell, _attach_label, _select_file, _render_file_list
  detail_pane.py         # DetailPaneMixin: _render_detail, _render_versions_section
  navigation.py          # NavigationMixin: _navigate_to, _on_back_clicked, _on_forward_clicked
  manifest_refresh.py    # ManifestRefreshMixin: _refresh_manifest_async, _refresh_on_focus
  upload_destination.py  # UploadDestinationMixin: _resolve_upload_destination
  cancel_progress.py     # CancelProgressMixin: _arm_cancel, _disarm_cancel,
                         #                       _on_cancel_clicked, _show_progress_no_cancel,
                         #                       _on_show_deleted_toggled
  downloads.py           # DownloadsMixin: _download_folder_name (static),
                         #                  _choose_download_destination,
                         #                  _prompt_existing_destination, _start_download,
                         #                  _choose_version_destination,
                         #                  _prompt_existing_version_destination,
                         #                  _start_version_download
  uploads.py             # UploadsMixin: _start_upload, _choose_upload_source,
                         #                _maybe_prompt_conflict_then_upload,
                         #                _start_folder_upload, _choose_upload_folder_source
  delete_restore.py      # DeleteRestoreMixin: _run_delete_worker, _confirm_delete_file,
                         #                      _confirm_delete_folder, _confirm_restore_version,
                         #                      _confirm_and_delete
  quota.py               # QuotaMixin: _handle_quota_exceeded, _run_eviction_pass
  resume_banner.py       # ResumeBannerMixin: _refresh_resume_banner,
                         #                     _on_resume_cancel_clicked,
                         #                     _start_resume_pending
```

Lower priority than #6 / #8 / #10 because the *internal shape* is
already good (a class with discrete methods rather than 50 nested
closures). This refactor is cosmetic — it improves diff
reviewability but doesn't change behavior or testability the way
the original monolith → class conversion did. Pure mixin extraction
should fit the move-only refactor discipline cleanly; no test
contract changes expected.

## Recommended sequencing

1. **#7 `api_client.py`** — best test coverage already, lowest UI risk, demonstrates the pattern.
2. **#3 `poller.py`** — high-value: receive code is hairy and the split makes streaming/classic/fasttrack independently testable.
3. **#4 `vault_upload.py`** — low risk because the dataclasses already show the seams.
4. **#9 `vault.py`** — pulls recovery-kit out, sets up the eventual `vault/` folder.
5. **#5 `tray.py`** — gates a lot of UI; do it after the receive-side splits stabilize.
6. **#1, #2, #6, #8, #10** — the GTK windows. Do these last and one PR per file; each has many subtle GTK lifetime gotchas (signal connections, Adw widget parents) that benefit from focused review.
7. **#11** — `windows_vault_browser/app.py` mixin extraction. Lowest priority: pure cosmetic split, no behavior change, but the file is large enough that diff readability suffers.

Each refactor is a single PR titled `refactor(<area>): split <file> into <package>`. Behavior must be byte-identical; verify by running `./test_loop.sh` plus the relevant vault tests in [`docs/testing/vault-tests.md`](../testing/vault-tests.md).

## Full file list, sorted by size (largest first)

Bytes per file. 146 files total.

```
107164  desktop/src/windows_vault.py
 82555  desktop/src/windows_vault_browser.py
 72113  desktop/src/poller.py
 57982  desktop/src/vault_upload.py
 54932  desktop/src/tray.py
 53959  desktop/src/vault_folders_tab.py
 46927  desktop/src/api_client.py
 45417  desktop/src/windows_history.py
 45231  desktop/src/vault.py
 37915  desktop/src/windows_settings.py
 37521  desktop/src/vault_download.py
 36215  desktop/src/config.py
 36068  desktop/src/vault_crypto.py
 35514  desktop/src/vault_manifest.py
 29196  desktop/src/windows_pairing.py
 29031  desktop/src/vault_binding_twoway.py
 26986  desktop/src/vault_migration_runner.py
 25531  desktop/src/vault_binding_sync.py
 23198  desktop/src/vault_restore.py
 22988  desktop/src/windows_send.py
 22653  desktop/src/vault_import.py
 22401  desktop/src/vault_bindings.py
 22277  desktop/src/vault_runtime.py
 22083  desktop/src/vault_eviction.py
 21855  desktop/src/vault_export.py
 21316  desktop/src/windows_vault_import.py
 20159  desktop/src/windows_find_phone.py
 19365  desktop/src/bootstrap/appimage_relocate.py
 18222  desktop/src/file_manager_integration.py
 18160  desktop/src/crypto.py
 18128  desktop/src/vault_grant.py
 17139  desktop/src/receive_actions.py
 16941  desktop/src/vault_browser_model.py
 16755  desktop/src/history.py
 16216  desktop/src/vault_filesystem_watcher.py
 15626  desktop/src/connection.py
 14791  desktop/src/find_device_responder.py
 14388  desktop/packaging/appimage/build.sh
 14086  desktop/packaging/appimage/build-appimage.sh
 13799  desktop/src/brand.py
 13654  desktop/packaging/appimage/.tools/linuxdeploy-plugin-gtk.sh
 13465  desktop/src/vault_integrity.py
 13220  desktop/install-from-source.sh
 13096  desktop/src/vault_import_runner.py
 13017  desktop/src/secrets.py
 13000  desktop/src/vault_binding_lifecycle.py
 12365  desktop/src/vault_local_index.py
 12107  desktop/src/vault_binding_baseline.py
 11428  desktop/src/pairing_key.py
 11190  desktop/install.sh
 11018  desktop/src/vault_grant_wrap.py
 10690  desktop/src/vault_delete.py
 10594  desktop/src/vault_debug_bundle.py
 10570  desktop/src/vault_activity.py
 10506  desktop/src/windows_onboarding.py
 10495  desktop/src/updater/update_runner.py
 10083  desktop/src/vault_purge_schedule.py
  9860  desktop/src/updater/version_check.py
  9616  desktop/src/backends/linux/location_backend.py
  9425  desktop/src/devices.py
  9397  desktop/src/vault_connect_folder_dialog.py
  9278  desktop/src/main.py
  9246  desktop/src/vault_local_state.py
  9222  desktop/src/vault_grant_qr.py
  9087  desktop/src/vault_repair.py
  9026  desktop/src/vault_migration.py
  9019  desktop/src/vault_folder_runtime.py
  8386  desktop/src/pairing.py
  7989  desktop/src/vault_passphrase.py
  7989  desktop/src/find_device_alert.py
  7753  desktop/src/vault_runtime_watchers.py
  7474  desktop/src/runners/send_runner.py
  7238  desktop/src/bootstrap/dependency_check.py
  6841  desktop/uninstall.sh
  6607  desktop/src/vault_ransomware_detector.py
  6525  desktop/src/vault_binding_preflight.py
  6458  desktop/src/bootstrap/appimage_migration.py
  6457  desktop/src/vault_clear.py
  6253  desktop/src/vault_access_rotation.py
  6144  desktop/src/bootstrap/appimage_onboarding.py
  6043  desktop/README.md
  5999  desktop/src/vault_atomic.py
  5801  desktop/src/vault_usage.py
  5795  desktop/src/vault_logging.py
  5787  desktop/src/vault_export_reminder.py
  5539  desktop/src/bootstrap/appimage_install_hook.py
  5434  desktop/packaging/appimage/AppRun.sh
  5315  desktop/src/vault_error_messages.py
  5130  desktop/src/vault_conflict_naming.py
  4855  desktop/src/windows.py
  4822  desktop/src/clipboard.py
  4804  desktop/src/vault_binding_scan.py
  4466  desktop/src/vault_ui_state.py
  4359  desktop/src/vault_folder_actions.py
  4023  desktop/src/vault_migration_propagation.py
  3884  desktop/src/dialogs.py
  3598  desktop/src/vault_relay_errors.py
  3598  desktop/src/runners/receiver_runner.py
  3580  desktop/packaging/appimage/README.md
  3576  desktop/src/vault_folder_ui_state.py
  3513  desktop/src/windows_common.py
  3461  desktop/src/vault_trash.py
  3324  desktop/src/vault_window_args.py
  2818  desktop/src/bootstrap/args.py
  2524  desktop/src/bootstrap/logging_setup.py
  1922  desktop/src/bootstrap/startup_context.py
  1885  desktop/src/notifications.py
  1773  desktop/src/backends/linux/shell_backend.py
  1710  desktop/nautilus-send-to-phone.py
  1592  desktop/src/messaging/fn_transfer_adapter.py
  1542  desktop/src/vault_time_format.py
  1492  desktop/src/interfaces/location.py
  1456  desktop/src/runners/registration_runner.py
  1362  desktop/src/vault_bytes_format.py
  1166  desktop/src/messaging/fasttrack_adapter.py
  1151  desktop/src/platform/linux/compose.py
  1114  desktop/src/runners/pairing_runner.py
  1052  desktop/packaging/appimage/linuxdeploy.recipe.sh
  1030  desktop/src/bootstrap/app_version.py
   873  desktop/requirements.txt
   833  desktop/src/platform/compose.py
   833  desktop/src/backends/linux/dialog_backend.py
   777  desktop/src/platform/contract/desktop_platform.py
   769  desktop/src/backends/linux/notification_backend.py
   664  desktop/src/messaging/dispatcher.py
   614  desktop/src/backends/linux/clipboard_backend.py
   517  desktop/src/interfaces/dialogs.py
   475  desktop/src/platform/__init__.py
   453  desktop/src/updater/__init__.py
   453  desktop/src/platform/contract/capabilities.py
   451  desktop/src/messaging/message_types.py
   444  desktop/src/messaging/message_model.py
   422  desktop/src/interfaces/notifications.py
   392  desktop/src/messaging/__init__.py
   390  desktop/src/interfaces/clipboard.py
   371  desktop/src/interfaces/shell.py
   148  desktop/src/platform/contract/__init__.py
   128  desktop/src/platform/linux/__init__.py
    94  desktop/src/__main__.py
    66  desktop/src/backends/__init__.py
    62  desktop/VERSION.md
    57  desktop/src/interfaces/__init__.py
    37  desktop/src/backends/linux/__init__.py
     0  desktop/src/runners/__init__.py
     0  desktop/src/__init__.py
     0  desktop/src/bootstrap/__init__.py
```
