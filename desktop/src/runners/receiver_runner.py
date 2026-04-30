"""Receiver startup runner for tray/headless modes."""

from __future__ import annotations

import logging
import signal
import threading

from ..api_client import ApiClient
from ..config import Config
from ..connection import ConnectionManager, ConnectionState
from ..crypto import KeyManager
from ..find_device_alert import GtkSubprocessAlert
from ..platform import DesktopPlatform
from ..poller import Poller

log = logging.getLogger("desktop-connector")


def run_receiver(
    config: Config,
    crypto: KeyManager,
    headless: bool,
    platform: DesktopPlatform,
) -> None:
    """Run the receiver loop (with tray or headless)."""
    from ..history import TransferHistory

    conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
    api = ApiClient(conn, crypto)
    history = TransferHistory(config.config_dir)

    # M.8: GTK4 subprocess alert for incoming locate requests. Headless
    # runs skip the modal entirely (no GTK session) but still get the
    # responder's heartbeat updates. The on_user_stop callback closes
    # over the about-to-be-created responder via a forward indirection.
    alert: GtkSubprocessAlert | None = None
    if not headless:
        alert = GtkSubprocessAlert(
            config_dir=config.config_dir,
            on_user_stop=lambda: poller._find_device_responder.stop(),
        )

    poller = Poller(
        config, conn, api, crypto, history, platform,
        find_device_alert=alert,
    )

    # Wire up notifications
    poller.on_file_received(platform.notifications.notify_file_received)

    last_notified = [None]  # "connected", "disconnected", or None (never notified)

    def on_state_change(state):
        if state == ConnectionState.CONNECTED and last_notified[0] != "connected":
            if last_notified[0] == "disconnected":
                platform.notifications.notify_connection_restored()
            last_notified[0] = "connected"
        elif (
            state == ConnectionState.DISCONNECTED
            and last_notified[0] != "disconnected"
        ):
            if last_notified[0] == "connected":
                platform.notifications.notify_connection_lost()
            last_notified[0] = "disconnected"

    conn.on_state_change(on_state_change)

    # Initial connection check
    conn.check_connection()

    # Start poller in background thread
    poller_thread = threading.Thread(target=poller.run, daemon=True, name="poller")
    poller_thread.start()

    if headless:
        log.info("Running headless receiver. Saving to: %s", config.save_directory)
        log.info("Press Ctrl+C to stop.")
        shutdown = threading.Event()

        def handle_signal(*_):
            log.info("Shutting down...")
            poller.stop()
            shutdown.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        shutdown.wait()
        return

    from ..tray import TrayApp

    tray = TrayApp(
        conn,
        poller,
        api,
        config,
        crypto,
        history,
        config.save_directory,
        platform,
    )
    log.info("Starting tray icon. Saving to: %s", config.save_directory)

    def handle_signal(*_):
        log.info("Shutting down...")
        poller.stop()
        tray.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    tray.run()  # Blocks main thread
