"""TrayApp orchestrator — composes the topical mixins and owns lifecycle.

GTK4 windows run as subprocesses to avoid GTK3/4 conflict (pystray
loads GTK3); see ``open_window.py`` for the launchers. The icon-poll
loop here drives the pixbuf swap based on connection / upload state
+ wakes the on-demand ping each tick.
"""

import logging
import os
import threading
import time
from pathlib import Path

from ..api_client import ApiClient
from ..config import Config
from ..connection import ConnectionManager, ConnectionState
from ..crypto import KeyManager
from ..history import TransferHistory
from ..platform import DesktopPlatform
from ..poller import Poller
from ..updater import version_check
from .icon_assets import _bake_state_paths, _make_icon
from .open_window import OpenWindowMixin
from .ping import PingMixin
from .repair import RepairMixin
from .send_clipboard import SendClipboardMixin
from .status import StatusMixin
from .update_check import UpdateCheckMixin
from .vault_submenu import VaultSubmenuMixin

log = logging.getLogger(__name__)


class TrayApp(
    StatusMixin,
    RepairMixin,
    PingMixin,
    OpenWindowMixin,
    SendClipboardMixin,
    UpdateCheckMixin,
    VaultSubmenuMixin,
):

    def __init__(self, connection: ConnectionManager, poller: Poller,
                 api: ApiClient, config: Config, crypto: KeyManager,
                 history: TransferHistory, save_dir: Path,
                 platform: DesktopPlatform):
        self.conn = connection
        self.poller = poller
        self.api = api
        self.config = config
        self.crypto = crypto
        self.history = history
        self.save_dir = save_dir
        self.platform = platform
        self._icon = None
        self._should_quit = threading.Event()
        self._was_uploading = False
        self._remote_online = False
        self._fcm_available = False
        self._fcm_checked = False
        # Update-check state (P.6b). _update_info is the latest result from
        # version_check.check_for_update(); _running_appimage caches whether
        # we're inside an AppImage so the menu visibility lambdas don't
        # re-read os.environ on every menu open.
        self._update_info: version_check.UpdateInfo | None = None
        self._running_appimage = bool(os.environ.get("APPIMAGE"))
        # Vault watcher runtime — lazily started when the vault is open
        # and there's at least one bound binding. The runtime owns watch-
        # dog observers and per-binding ransomware detectors; on tripped
        # verdicts it pauses the binding via the lifecycle helper. F-Y13.
        self._vault_watcher_runtime = None  # type: ignore[assignment]
        self._vault_watcher_lock = threading.Lock()
        # F-LT06: tray-side autosync loop. Without this the watchdog
        # observer's events sit in the in-memory debouncer forever:
        # WatcherCoordinator.tick() drains debounced events into the
        # store's pending-ops queue, and flush_and_sync_binding consumes
        # those ops + does a catch-up directory walk for changes that
        # landed while no watcher was up. Both were only triggered by
        # manual Sync now before this loop existed. Set on first
        # _ensure_vault_watcher_runtime success; the daemon thread
        # ticks every VAULT_AUTOSYNC_INTERVAL_S until shutdown.
        self._vault_autosync_runtime = None  # type: ignore[assignment]
        self._vault_autosync_started = False
        self._vault_autosync_kick = threading.Event()
        # F-LT07: set while a flush_and_sync_binding call is actively
        # uploading chunks / publishing the manifest, so the tray icon
        # paints the yellow-with-blue-border "uploading" sparkle the
        # transfer pipeline already uses for outgoing transfers.
        self._vault_autosync_active = False

    def run(self) -> None:
        try:
            import pystray
        except ImportError:
            log.warning("pystray not available, running without tray icon")
            self._should_quit.wait()
            return

        def build_menu():
            # Refresh icon on every menu open to fix stale icons
            self._update_icon()
            return pystray.Menu(
                pystray.MenuItem(
                    lambda _: self._status_text(),
                    None,
                    enabled=False,
                ),
                pystray.MenuItem(
                    lambda _: self._auth_banner_text(),
                    self._repair,
                    visible=lambda _: self.conn.auth_failure_kind is not None,
                ),
                pystray.MenuItem(
                    lambda _: "⚠ Server storage full — delivery waiting",
                    None,
                    enabled=False,
                    visible=lambda _: (self.conn.storage_full
                                      and self.conn.auth_failure_kind is None),
                ),
                # H.5: rendered when the OS keyring isn't reachable and
                # secrets are sitting in plaintext config.json. Click
                # opens an explainer window with what / why / how-to-fix.
                pystray.MenuItem(
                    lambda _: "⚠ Secrets in plaintext — click for info",
                    self._show_secret_storage_warning,
                    visible=lambda _: not self.config.is_secret_storage_secure(),
                ),
                pystray.MenuItem(
                    "Force Reconnect",
                    self._try_now,
                    visible=lambda _: (self.conn.state != ConnectionState.CONNECTED
                                      and self.conn.auth_failure_kind is None
                                      and not self.conn.storage_full),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Send Files...", self._send_files, visible=lambda _: self.config.is_paired),
                pystray.MenuItem(
                    "Send Clipboard",
                    self._send_clipboard,
                    visible=lambda _: self.config.is_paired and self.platform.capabilities.clipboard_text,
                ),
                pystray.MenuItem("Find my Device", self._find_phone,
                                 visible=lambda _: self.config.is_paired and self._fcm_available),
                pystray.MenuItem("Show History", self._show_history),
                pystray.MenuItem(
                    "Open Save Folder",
                    self._open_folder,
                    visible=lambda _: self.platform.capabilities.open_folder,
                ),
                pystray.Menu.SEPARATOR,
                # Always available — multi-device support means the user can
                # add another pairing on top of an existing one. The pairing
                # window's naming step handles uniqueness against the
                # current pair list.
                pystray.MenuItem(
                    lambda _: ("Pair..." if not self.config.is_paired
                               else "Pair another device..."),
                    self._pair,
                ),
                # Vault submenu (T3.5). Visibility + contents driven by
                # the pure helpers in vault_ui_state so behavior matches
                # the §D16 lock without knowing about pystray.
                pystray.MenuItem(
                    "Vault",
                    self._build_vault_submenu(),
                    visible=lambda _: self._vault_submenu_visible(),
                ),
                pystray.MenuItem("Settings...", self._show_settings),
                # Update items appear only inside an AppImage — apt-pip and
                # dev-tree installs can't act on an in-app update anyway.
                pystray.MenuItem(
                    lambda _: f"Update available → {self._update_info.latest_version}"
                              if self._update_info else "",
                    pystray.Menu(
                        pystray.MenuItem("Install update", self._install_update),
                        pystray.MenuItem("View release notes", self._open_release_notes),
                        pystray.MenuItem("Dismiss this version", self._dismiss_update),
                    ),
                    visible=lambda _: self._has_pending_update(),
                ),
                pystray.MenuItem(
                    "Check for updates",
                    self._manual_update_check,
                    visible=lambda _: self._running_appimage,
                ),
                pystray.MenuItem("Quit", self._quit),
            )

        self._icon = pystray.Icon(
            "desktop-connector",
            icon=_make_icon(self._current_state_key()),
            title="Desktop Connector",
            menu=build_menu(),
        )

        # Stable on-disk paths keyed by state. Bypasses pystray's delete +
        # mktemp path churn on every swap, which is what produced the
        # default-icon flash between state transitions.
        self._state_paths = _bake_state_paths(self.config.config_dir / "tray")
        self._last_applied_state: str | None = None

        def _on_state_change(_state):
            # Repaint the icon AND rebuild the menu. On AppIndicator backends
            # (libappindicator / StatusNotifierItem) the menu is D-Bus-bound
            # and lambda labels (e.g. _status_text) are only re-evaluated on
            # update_menu(); without this rebuild the menu kept showing the
            # last-rendered "Online" while the icon flipped to disconnected.
            self._update_icon()
            try:
                if self._icon:
                    self._icon.update_menu()
            except Exception:
                pass
        self.conn.on_state_change(_on_state_change)

        def _on_auth_failure(kind):
            log.warning("auth.failure.surface kind=%s", kind.value)
            # effective_state just flipped — repaint the tray icon (blue → orange)
            # alongside the menu banner.
            self._update_icon()
            try:
                self._icon.update_menu()
            except Exception:
                pass
            try:
                self.platform.notifications.notify(
                    "Pairing lost",
                    "Server no longer recognises this device. Click the tray icon to re-pair.",
                )
            except Exception:
                log.exception("auth failure notification error")
        self.conn.on_auth_failure(_on_auth_failure)

        def _on_storage_full(flagged: bool):
            try:
                self._icon.update_menu()
            except Exception:
                pass
            if flagged:
                try:
                    self.platform.notifications.notify(
                        "Server storage full",
                        "A send is waiting until the recipient has finished downloading earlier transfers.",
                    )
                except Exception:
                    log.exception("storage full notification error")
        self.conn.on_storage_full_change(_on_storage_full)

        # Poll for state changes that affect icon or menu
        self._upload_status_file = self.config.config_dir / "upload_active.json"
        self._was_uploading = False
        self._was_paired = self.config.is_paired
        self._remote_online = False
        # Vault submenu state — `vault_active` is mutated by the
        # settings subprocess and `vault_exists` flips when the wizard
        # subprocess writes `last_known_id`. Both must be polled because
        # pystray takes a snapshot of `visible=` lambdas at icon
        # construction; without an explicit `update_menu()` call the
        # submenu doesn't refresh until app restart.
        self._was_vault_active = self.config.vault_active
        self._was_vault_exists = self._local_vault_exists()
        self._last_ping_time = 0.0
        self._ping_in_flight = False
        self._ping_lock = threading.Lock()
        # 5 min between probes: icon may be stale, but phone battery stays near-zero.
        # On-connect triggers an immediate ping regardless.
        self._ping_interval = 300.0

        def icon_poll():
            was_connected = False
            while not self._should_quit.is_set():
                changed = False

                # "Outgoing" = uploading OR delivering. Covers the full
                # uploading → delivering → delivered arc, plus a vault
                # autosync flush actively writing to the relay (F-LT07).
                outgoing = (self._upload_status_file.exists()
                            or self.poller.has_live_outgoing()
                            or self._vault_autosync_active)
                if outgoing != self._was_uploading:
                    self._was_uploading = outgoing
                    self._update_icon()
                    changed = True

                paired = self.config.is_paired
                if paired != self._was_paired:
                    self._was_paired = paired
                    changed = True

                # Vault submenu state — both inputs to vault_ui_state
                # decision functions. config.vault_active reloads from
                # disk on read (see config.py vault_active getter), so
                # the settings subprocess's writes propagate here on
                # the next 2-second tick.
                vault_active_now = self.config.vault_active
                if vault_active_now != self._was_vault_active:
                    self._was_vault_active = vault_active_now
                    changed = True
                    self._ensure_vault_watcher_runtime()
                vault_exists_now = self._local_vault_exists()
                if vault_exists_now != self._was_vault_exists:
                    self._was_vault_exists = vault_exists_now
                    changed = True
                    self._ensure_vault_watcher_runtime()

                # One-time FCM availability check on first connection
                if not self._fcm_checked and self.conn.state == ConnectionState.CONNECTED:
                    try:
                        self._fcm_available = self.api.check_fcm_available()
                        self._fcm_checked = True
                        if self._fcm_available:
                            changed = True
                    except Exception:
                        pass

                connected = self.conn.state == ConnectionState.CONNECTED
                if connected:
                    # On a transition back to CONNECTED, use the same 30 s
                    # cache window as the menu-open ping rather than forcing
                    # an unconditional ping (min_age=0). Otherwise every
                    # transient wifi-reassoc / DHCP-renew / route-flap blip
                    # cascades into a fresh FCM ping wake on the phone —
                    # 1 modem-active session per blip — even though we
                    # pinged seconds ago and the result is still meaningful.
                    just_connected = not was_connected
                    min_age = 30.0 if just_connected else self._ping_interval
                    self._maybe_ping(min_age)
                elif self._remote_online:
                    self._remote_online = False
                    self._update_icon()
                was_connected = connected

                if changed:
                    try:
                        self._icon.update_menu()
                    except Exception:
                        pass

                time.sleep(2)
        threading.Thread(target=icon_poll, daemon=True).start()

        # In-app updater (P.6b): one check on boot + once every 24 h.
        # version_check itself caches at the same TTL, so the network is
        # only hit when truly due. Outside an AppImage the loop is a no-op.
        if self._running_appimage:
            threading.Thread(target=self._update_check_loop, daemon=True).start()

        # Boot-time vault watcher start (F-Y13). Idempotent — the
        # watcher hooks above also call this when the vault toggle flips.
        try:
            self._ensure_vault_watcher_runtime()
        except Exception:  # noqa: BLE001
            log.exception("vault.sync.watcher_runtime_boot_failed")

        self._icon.run()

    def stop(self) -> None:
        self._should_quit.set()
        if self._icon:
            self._icon.stop()

    def _try_now(self, *_) -> None:
        self.conn.try_now()
        self.poller.wake()

    def _quit(self, *_) -> None:
        self.poller.stop()
        self.stop()
