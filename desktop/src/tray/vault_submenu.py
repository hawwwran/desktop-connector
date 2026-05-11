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


class VaultSubmenuMixin:
    def _vault_submenu_visible(self) -> bool:
        """The submenu is visible when the user has vault.active=True."""
        from ..vault_ui_state import should_show_vault_submenu
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
        from ..vault.grant.grant import local_vault_grant_exists

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
                "Open Vault…",
                self._spawn_vault_browser,
                visible=lambda _: self._vault_submenu_entry_visible("open_vault"),
            ),
            pystray.MenuItem(
                "Sync now",
                self._vault_sync_now_stub,
                visible=lambda _: self._vault_submenu_entry_visible("sync_now"),
            ),
            pystray.MenuItem(
                "Export…",
                self._vault_export_stub,
                visible=lambda _: self._vault_submenu_entry_visible("export"),
            ),
            pystray.MenuItem(
                "Import…",
                self._vault_import_stub,
                visible=lambda _: self._vault_submenu_entry_visible("import"),
            ),
            pystray.MenuItem(
                "Settings",
                self._spawn_vault_main,
                visible=lambda _: self._vault_submenu_entry_visible("settings"),
            ),
        )

    def _vault_submenu_entry_visible(self, token: str) -> bool:
        from ..vault_ui_state import vault_submenu_entries
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

    def _vault_sync_now_stub(self, *_) -> None:
        # F-U20: surface where the real "Sync now" lives so the click
        # doesn't feel like a dead button. The backend is in Vault
        # settings → Folders → Sync now per binding.
        log.info("vault.tray.sync_now.stub")
        try:
            self.platform.notifications.notify(
                title="Vault — Sync now",
                body="Open Vault Settings → Folders → Sync now per binding.",
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
                    self._vault_watcher_runtime = VaultWatcherRuntime(
                        vault_id=vault_id,
                        store=store,
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

            for binding in active_bindings:
                if self._should_quit.is_set():
                    return
                # F-LT07: paint the "uploading" sparkle for the
                # duration of each flush. Most no-op ticks finish
                # within ~100 ms (just a directory walk) and the
                # icon-poll's 2 s cadence won't even register them;
                # real uploads stretch over seconds and flip the
                # icon yellow until they finish.
                self._vault_autosync_active = True
                result = None
                try:
                    result = autosync_runtime.flush_and_sync_binding(
                        binding_id=binding.binding_id,
                        author_device_id=author_device_id,
                        device_name=device_name,
                        should_continue=lambda: not self._should_quit.is_set(),
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "vault.sync.autosync_flush_failed binding=%s",
                        binding.binding_id,
                    )
                finally:
                    self._vault_autosync_active = False
                if result is None:
                    continue
                outcomes = getattr(result, "outcomes", []) or []
                if outcomes:
                    log.info(
                        "vault.sync.autosync.flushed binding=%s ops=%d",
                        binding.binding_id, len(outcomes),
                    )

    def _vault_export_stub(self, *_) -> None:
        log.info("vault.tray.export.stub")
        try:
            self.platform.notifications.notify(
                title="Vault — Export",
                body="Open Vault Settings → Recovery → Export… to back up your vault.",
            )
        except Exception:  # noqa: BLE001
            log.exception("vault.tray.export.notify_failed")

    def _vault_import_stub(self, *_) -> None:
        log.info("vault.tray.import.stub")
        try:
            self.platform.notifications.notify(
                title="Vault — Import",
                body="Use Vault Settings → Recovery → Import to load a vault bundle.",
            )
        except Exception:  # noqa: BLE001
            log.exception("vault.tray.import.notify_failed")
