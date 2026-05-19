"""Vault submenu (T3.5) + tray-side autosync loop (F-LT06).

Submenu visibility + items are driven by the pure helpers in
``vault_ui_state`` so the §D16 routing rules stay reusable. Submenu
contents are static (every possible item registered up front) and per-
item visibility lambdas gate the right ones in.

The autosync loop runs as a daemon thread once a vault is open + at
least one binding is bound. One pass = drain debounced watcher events
into the pending-ops queue, then ``flush_and_sync_binding`` per active
binding (catch-up scan + queue dispatch). Loop interval is
``VAULT_AUTOSYNC_INTERVAL_S``; ``_vault_autosync_kick`` wakes it
early on watcher start so the offline-catch-up runs immediately.
"""

import logging
import threading

log = logging.getLogger(__name__)

# F-LT06: how often the tray drives a vault autosync pass — drains the
# watcher debouncer + runs flush_and_sync_binding for each active
# binding. Real changes drive responsiveness via ``_vault_autosync_kick``
# (watchers fire it on inotify/FSEvents) so this interval is just the
# no-op backstop catch-up cadence. A short interval here just produces
# extra /api/vaults/.../manifest fetches per minute with nothing to do,
# which amplifies any transient local network flakiness into a retry
# storm and feeds extra FCM ping wakes to the phone via the reconnect
# path. 60 s is comfortably above the typical wifi-reassoc/DHCP-renew
# blip while still bounded enough to recover from a missed watcher event.
VAULT_AUTOSYNC_INTERVAL_S = 60.0

# §4.M1 — orphan-chunk reaper minimum gap between passes.
# Each pass paginates the full server-side chunk list (potentially
# many KB of metadata over the wire), so we don't want to run it
# every 60 s tick — orphans accumulate at most ~one batch's worth
# per CAS-exhaust event, so an hourly cadence catches them long
# before the server's 30-day retention sweep does.
VAULT_ORPHAN_REAP_INTERVAL_S = 3600.0


class VaultSubmenuMixin:
    def _vault_submenu_visible(self) -> bool:
        """The submenu is visible when the user has vault.active=True."""
        from ..vault.ui.ui_state import should_show_vault_submenu
        return should_show_vault_submenu(self.config.vault_active)

    def _local_vault_exists(self) -> bool:
        """F-U15: authoritative — a vault exists locally iff
        ``config['vault']['last_known_id']`` is set **and** the grant
        store actually has an unlock entry for that id.

        The id-only heuristic (T3 era) admits a stale-config race: if
        a grant artifact gets deleted out from under the config (manual
        keyring purge, OS-keyring switch, edge-case wizard cleanup),
        the tray would still show Open / Sync / Settings and the user
        clicks into a doomed unlock flow. Cross-checking against the
        grant store flips the submenu back to Create / Import — the
        right recovery affordance.

        Calls ``self.config.reload()`` first because the wizard
        subprocess writes ``last_known_id`` and the tray needs to see
        it to flip its submenu from Create/Import to operating mode.
        Same propagation pattern as ``Config.vault_active``.
        """
        from ..vault.grant.store import local_vault_grant_exists

        self.config.reload()
        raw = self.config._data.get("vault")
        if not isinstance(raw, dict):
            return False
        vault_id = raw.get("last_known_id")
        if not vault_id:
            return False
        return local_vault_grant_exists(self.config.config_dir, vault_id)

    def _build_vault_submenu(self) -> "pystray.Menu":
        """Build the Vault submenu items based on the §D16 routing rules.

        Submenu contents are static — we register every possible item up
        front and gate visibility per item via lambdas. pystray rebuilds
        the menu on every refresh so the user sees the right entries.

        ``pystray`` is imported inside ``TrayApp.run()`` (lazy ImportError
        fallback for headless boxes); this method also imports it locally
        so callers from outside ``run`` don't NameError.
        """
        import pystray
        return pystray.Menu(
            pystray.MenuItem(
                "Create vault…",
                self._spawn_vault_wizard,
                visible=lambda _: self._vault_submenu_entry_visible("create_vault"),
            ),
            pystray.MenuItem(
                "Import vault…",
                self._spawn_vault_wizard,
                visible=lambda _: self._vault_submenu_entry_visible("import_vault"),
            ),
            pystray.MenuItem(
                "Add this device to a vault…",
                self._spawn_vault_join,
                visible=lambda _: self._vault_submenu_entry_visible("join_vault"),
            ),
            pystray.MenuItem(
                "Open Vault…",
                self._spawn_vault_browser,
                visible=lambda _: self._vault_submenu_entry_visible("open_vault"),
            ),
            pystray.MenuItem(
                "Sync now",
                self._vault_sync_now,
                visible=lambda _: self._vault_submenu_entry_visible("sync_now"),
            ),
            pystray.MenuItem(
                "Import…",
                self._spawn_vault_import,
                visible=lambda _: self._vault_submenu_entry_visible("import"),
            ),
            pystray.MenuItem(
                "Export…",
                self._spawn_vault_export,
                visible=lambda _: self._vault_submenu_entry_visible("export"),
            ),
            pystray.MenuItem(
                "Settings",
                self._spawn_vault_main,
                visible=lambda _: self._vault_submenu_entry_visible("settings"),
            ),
        )

    def _vault_submenu_entry_visible(self, token: str) -> bool:
        from ..vault.ui.ui_state import vault_submenu_entries
        if not self.config.vault_active:
            return False
        entries = vault_submenu_entries(
            toggle_active=self.config.vault_active,
            vault_exists=self._local_vault_exists(),
        )
        return token in entries

    def _spawn_vault_wizard(self, *_) -> None:
        self._open_gtk4_window("vault-onboard")

    def _spawn_vault_main(self, *_) -> None:
        self._open_gtk4_window("vault-main")

    def _spawn_vault_browser(self, *_) -> None:
        self._open_gtk4_window("vault-browser")

    def _spawn_vault_join(self, *_) -> None:
        self._open_gtk4_window("vault-join")

    def _vault_sync_now(self, *_) -> None:
        """Tray "Sync now" — kick the in-process autosync loop.

        Review §6.H3: pre-fix this fired a notification telling the
        user to open Vault Settings → Folders → Sync now per binding.
        The in-process autosync loop was already capable of doing the
        work; the kick event just needed to be wired to the menu so
        the click does what it advertises instead of bouncing the
        user into another window.

        ``_ensure_vault_watcher_runtime`` is idempotent — it starts
        the watcher + autosync threads on first call and is a no-op
        thereafter, so the first click after vault-open starts the
        pipeline before kicking it.
        """
        log.info("vault.tray.sync_now.kicked")
        try:
            self._ensure_vault_watcher_runtime()
            self._vault_autosync_kick.set()
        except Exception:  # noqa: BLE001
            log.exception("vault.tray.sync_now.kick_failed")
            try:
                self.platform.notifications.notify(
                    title="Vault — Sync now",
                    body=(
                        "Couldn't start the sync. Open Vault Settings "
                        "→ Folders to check the binding state."
                    ),
                )
            except Exception:  # noqa: BLE001
                log.exception("vault.tray.sync_now.notify_failed")
            return
        try:
            self.platform.notifications.notify(
                title="Vault — Sync now",
                body="Syncing your bound folders in the background.",
            )
        except Exception:  # noqa: BLE001
            log.exception("vault.tray.sync_now.notify_failed")

    def _ensure_vault_watcher_runtime(self) -> None:
        """Start filesystem watchers + ransomware detectors when the vault is open.

        Idempotent: re-calling either picks up newly-bound folders or is
        a no-op. Failures are logged and don't crash the tray — sync via
        the manual "Sync now" button still works.
        """
        with self._vault_watcher_lock:
            if not self.config.vault_active or not self._local_vault_exists():
                if self._vault_watcher_runtime is not None:
                    self._vault_watcher_runtime.stop_all()
                    self._vault_watcher_runtime = None
                self._vault_autosync_runtime = None
                return
            try:
                from ..vault.binding.runtime_watchers import VaultWatcherRuntime
                from ..vault.binding.bindings import VaultBindingsStore
                from ..vault.binding.lifecycle import BindingCancellationRegistry
                from ..vault.state.local_index import VaultLocalIndex
                from ..vault.folder.runtime import VaultRuntime
                vault_id = str(
                    self.config._data.get("vault", {}).get("last_known_id") or ""
                )
                if not vault_id:
                    return
                if self._vault_watcher_runtime is None:
                    local_index = VaultLocalIndex(self.config.config_dir)
                    store = VaultBindingsStore(local_index.db_path)
                    # Review §3.C2: share one registry between the watcher
                    # runtime (whose ransomware-trip handler calls
                    # pause_binding) and the autosync flush below — so a
                    # detector trip bails the in-flight cycle instead of
                    # letting it keep draining tombstones to completion.
                    self._vault_cancellation_registry = BindingCancellationRegistry()
                    self._vault_watcher_runtime = VaultWatcherRuntime(
                        vault_id=vault_id,
                        store=store,
                        cancellation_registry=self._vault_cancellation_registry,
                    )
                    # §A15 surface: notify-send toast when the detector
                    # trips. Without this the user sees only a generic
                    # "paused" state on the Folders tab — indistinguishable
                    # from a manual pause — and is likely to click Resume
                    # without realising encrypted payloads are about to
                    # flow up. Suite 0007 B4 caught the dead callback.
                    from ..vault.diagnostics.ransomware_detector import (
                        BANNER_BODY, BANNER_TITLE,
                    )

                    def _ransomware_notify(_binding_id: str) -> None:
                        self.platform.notifications.notify(
                            BANNER_TITLE, BANNER_BODY,
                            icon="dialog-warning",
                        )

                    self._vault_watcher_runtime.set_ransomware_callback(
                        _ransomware_notify,
                    )
                    # F-LT06: hold the GTK-free VaultRuntime alongside
                    # the watcher runtime so the autosync loop can call
                    # flush_and_sync_binding without re-deriving the
                    # serialization story.
                    self._vault_autosync_runtime = VaultRuntime(
                        config_dir=self.config.config_dir,
                        config=self.config,
                        vault_id=vault_id,
                        local_index=local_index,
                    )
                self._vault_watcher_runtime.start_for_active_bindings()
            except Exception:  # noqa: BLE001
                log.exception("vault.sync.watcher_runtime_init_failed")
                return

            # Start the autosync loop on first success; subsequent calls
            # just kick it so a newly-bound folder gets a catch-up scan
            # without waiting up to VAULT_AUTOSYNC_INTERVAL_S.
            if not self._vault_autosync_started:
                self._vault_autosync_started = True
                # Wake the loop the moment the connection recovers, so a
                # transient drop doesn't cost us up to a full interval
                # of catch-up lag. Registered once per process; the loop
                # itself is idempotent under spurious kicks.
                from ..connection import ConnectionState

                def _kick_on_reconnect(state: ConnectionState) -> None:
                    if state == ConnectionState.CONNECTED:
                        self._vault_autosync_kick.set()

                try:
                    self.conn.on_state_change(_kick_on_reconnect)
                except Exception:  # noqa: BLE001
                    log.exception("vault.sync.autosync_state_subscribe_failed")
                threading.Thread(
                    target=self._vault_autosync_loop,
                    name="vault-autosync",
                    daemon=True,
                ).start()
            self._vault_autosync_kick.set()

    def _vault_autosync_loop(self) -> None:
        """Background driver for vault binding autosync (F-LT06).

        One pass:
          1. ``WatcherCoordinator.tick()`` — drain debounced events
             into the pending-ops queue.
          2. ``flush_and_sync_binding(...)`` — runs the catch-up
             directory scan (handles "files placed while no watcher
             was up", which covers app-restart scenarios) and then
             dispatches the pending-ops queue.

        The loop runs every ``VAULT_AUTOSYNC_INTERVAL_S`` and wakes
        early when ``_vault_autosync_kick`` is set (e.g. on first
        watcher start so the offline-catch-up scan runs immediately,
        not 15 s later).

        Failures per-binding are logged and don't break the loop —
        the manual Sync now button still works as a backstop, and
        a CAS conflict from a concurrent settings-subprocess publish
        will resolve itself on the next tick.
        """
        from ..vault.binding.bindings import VaultBindingsStore
        from ..vault.state.local_index import VaultLocalIndex

        log.info(
            "vault.sync.autosync.started interval_s=%.1f",
            VAULT_AUTOSYNC_INTERVAL_S,
        )

        while not self._should_quit.is_set():
            # Kick takes priority over the periodic delay; if neither
            # fires we wait the full interval. Cleared after wake so
            # a single kick doesn't fire twice.
            woke_on_kick = self._vault_autosync_kick.wait(
                timeout=VAULT_AUTOSYNC_INTERVAL_S,
            )
            self._vault_autosync_kick.clear()
            if self._should_quit.is_set():
                return

            with self._vault_watcher_lock:
                watcher_runtime = self._vault_watcher_runtime
                autosync_runtime = self._vault_autosync_runtime

            if watcher_runtime is None or autosync_runtime is None:
                # Vault closed (or never opened on this boot); skip
                # this tick. Re-opening will set the kick again.
                continue

            # Skip the whole pass while the connection is down. Every
            # flush_and_sync_binding call would otherwise issue a doomed
            # /api/vaults/.../manifest fetch, hit the same Network is
            # unreachable error, and trip a backoff/reconnect cycle —
            # which on its own forces an extra FCM ping wake to the
            # phone. Watcher pending-ops + the catch-up filesystem scan
            # in flush_and_sync_binding both survive the gap, and the
            # state-change callback above will kick us the moment the
            # connection recovers.
            from ..connection import ConnectionState
            conn = getattr(self, "conn", None)
            if conn is not None and conn.state != ConnectionState.CONNECTED:
                continue

            try:
                watcher_runtime.tick_all()
            except Exception:  # noqa: BLE001
                log.exception("vault.sync.autosync_tick_failed")

            try:
                local_index = VaultLocalIndex(self.config.config_dir)
                store = VaultBindingsStore(local_index.db_path)
                bindings = store.list_bindings(vault_id=autosync_runtime.vault_id)
            except Exception:  # noqa: BLE001
                log.exception("vault.sync.autosync_list_bindings_failed")
                continue

            active_bindings = [
                b for b in bindings
                if b.state == "bound" and b.sync_mode != "paused"
            ]
            log.info(
                "vault.sync.autosync.tick reason=%s active_bindings=%d",
                "kick" if woke_on_kick else "interval",
                len(active_bindings),
            )

            author_device_id = self.config.device_id or ("0" * 32)
            device_name = (
                str(self.config.device_name or "").strip() or "this device"
            )

            cancellation_registry = getattr(
                self, "_vault_cancellation_registry", None,
            )
            for binding in active_bindings:
                if self._should_quit.is_set():
                    return
                # Review §3.C2: register this cycle on the shared
                # cancellation registry so the watcher runtime's
                # ransomware-trip handler (pause_binding via
                # registry.cancel) can interrupt it. The should_continue
                # closure consults both the global quit event AND this
                # per-binding event.
                cancel_event = (
                    cancellation_registry.register(binding.binding_id)
                    if cancellation_registry is not None
                    else None
                )
                result = None
                try:
                    result = autosync_runtime.flush_and_sync_binding(
                        binding_id=binding.binding_id,
                        author_device_id=author_device_id,
                        device_name=device_name,
                        should_continue=lambda ev=cancel_event: (
                            not self._should_quit.is_set()
                            and (ev is None or not ev.is_set())
                        ),
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "vault.sync.autosync_flush_failed binding=%s",
                        binding.binding_id,
                    )
                finally:
                    if cancellation_registry is not None:
                        cancellation_registry.clear(binding.binding_id)
                if result is None:
                    continue
                outcomes = getattr(result, "outcomes", []) or []
                if outcomes:
                    log.info(
                        "vault.sync.autosync.flushed binding=%s ops=%d",
                        binding.binding_id, len(outcomes),
                    )

            # Review §6.H1: at the end of every autosync tick, inspect
            # the local purge_state.json for any scheduled-purge whose
            # ``scheduled_for_epoch`` has elapsed. Pre-fix the schedule
            # was a dialog promise with NO executor — list_due_purges
            # had zero callers. The full automated flow would need
            # purge_secret persisted at schedule time (out of scope —
            # tracked in docs/plans/unfinished.md). What we wire today: detect
            # the due record, log the event, and surface a notification
            # so the user knows to reopen Vault Settings → Danger to
            # complete the purge. That converts the silent-no-fire bug
            # into an honest "purge ready, attend the desktop" signal.
            try:
                self._handle_due_purges_for_tick()
            except Exception:  # noqa: BLE001
                log.exception("vault.sync.autosync_purge_check_failed")

            # §4.M1: orphan-chunk reaper. Subtracts the manifest's
            # referenced chunks from the relay's stored chunk set
            # and DELETEs the diff. Throttled to hourly via
            # ``VAULT_ORPHAN_REAP_INTERVAL_S`` so each tick doesn't
            # paginate the full chunk list on cold runs.
            try:
                self._reap_orphans_for_tick(autosync_runtime)
            except Exception:  # noqa: BLE001
                log.exception("vault.sync.autosync_orphan_reap_failed")

    def _reap_orphans_for_tick(self, autosync_runtime) -> None:
        """§4.M1: hourly orphan-chunk reap from the autosync tick.

        Wall-clock throttle via :attr:`_vault_last_orphan_reap_at`
        keeps the cost bounded — orphans can only arise from
        CAS-exhaust on chunk uploads, migration cancel, or other
        interrupted flows. At most one batch's worth (~<100 chunks)
        per event, so an hourly cadence reclaims them long before
        the relay's 30-day retention pass does.

        Falls through silently on any error — the manual eviction
        pass remains the backstop, and a transient relay outage
        shouldn't break the loop.
        """
        import time as _time
        last_at = getattr(self, "_vault_last_orphan_reap_at", 0.0)
        now = _time.monotonic()
        if now - last_at < VAULT_ORPHAN_REAP_INTERVAL_S:
            return
        self._vault_last_orphan_reap_at = now

        vault_id = autosync_runtime.vault_id
        if not vault_id:
            return

        from ..vault.binding.runtime import (
            create_vault_relay,
            open_local_vault_from_grant,
        )
        from ..vault.ops.eviction import reap_orphan_chunks

        try:
            self.config.reload()
            relay = create_vault_relay(self.config)
            vault = open_local_vault_from_grant(
                self.config.config_dir, self.config, vault_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("vault.sync.autosync_orphan_reap_open_failed")
            return

        try:
            device_id = str(getattr(self.config, "device_id", "") or "0" * 32)
            result = reap_orphan_chunks(
                vault=vault, relay=relay,
                author_device_id=device_id,
            )
            if result.deleted_chunk_ids:
                log.info(
                    "vault.sync.autosync.orphan_reap_landed vault=%s "
                    "deleted=%d bytes_freed=%d",
                    vault_id[:12], len(result.deleted_chunk_ids),
                    result.bytes_freed,
                )
        except Exception:  # noqa: BLE001
            log.exception("vault.sync.autosync_orphan_reap_exception")
        finally:
            try:
                vault.close()
            except Exception:  # noqa: BLE001
                pass

    def _handle_due_purges_for_tick(self) -> None:
        """Notify on any due scheduled-purge (review §6.H1).

        Suite 0007 B1 (2026-05-19) caught this firing
        ``AttributeError: 'PendingPurge' object has no attribute
        'vault_id_dashed'`` — the dataclass field is ``vault_id``,
        which holds the dashed form per the docstring at
        ``purge_schedule.py:74``. The autosync wrapper caught it as
        ``vault.sync.autosync_purge_check_failed`` so the loop survived,
        but the user-facing notification never fired and the §6.H1
        wiring was silently dead.
        """
        from ..vault.ops.purge_schedule import list_due_purges
        notified = getattr(self, "_vault_purge_notified", set())
        if not isinstance(notified, set):
            notified = set()
        due = list_due_purges(self.config.config_dir)
        if not due:
            return
        for pending in due:
            key = (pending.vault_id, pending.job_id)
            if key in notified:
                continue
            notified.add(key)
            log.warning(
                "vault.purge.due_awaiting_user vault=%s job_id=%s scheduled_for=%s",
                pending.vault_id, pending.job_id,
                pending.scheduled_for_epoch,
            )
            try:
                self.platform.notifications.notify(
                    title="Vault — Hard purge is due",
                    body=(
                        "The hard purge you scheduled for this vault is "
                        "now due. Open Vault Settings → Danger zone to "
                        "complete the purge with the recovery kit. The "
                        "schedule stays armed until you confirm or "
                        "cancel it."
                    ),
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "vault.purge.notify_failed vault=%s",
                    pending.vault_id,
                )
        self._vault_purge_notified = notified

    def _spawn_vault_import(self, *_) -> None:
        # T8 ships an end-to-end import wizard (windows_vault_import.py)
        # that takes a `.dc-vault-export` bundle + passphrase, previews
        # the merge plan against the §D9 default `rename` resolution,
        # and publishes. Pre-2026-05-12 this tray entry routed to
        # ``_vault_import_stub`` which only fired a notification
        # pointing at "Vault Settings → Recovery → Import" — a path
        # that doesn't exist. The wizard subprocess is the real path.
        self._open_gtk4_window("vault-import")

    def _spawn_vault_export(self, *_) -> None:
        # §6.H3: bundle write + verify + optional shred. The pre-2026-05-18
        # tray entry was removed when only a notification stub existed;
        # the wizard subprocess is the real path.
        self._open_gtk4_window("vault-export")
