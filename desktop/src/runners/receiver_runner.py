"""Receiver startup runner for tray/headless modes."""

from __future__ import annotations

import logging
import signal
import threading

from ..api_client import ApiClient
from ..config import Config
from ..connection import ConnectionManager, ConnectionState
from ..crypto import KeyManager
from ..backends.linux.notification_backend import LinuxNotificationBackend
from ..interfaces.notifications import NotificationBackend
from ..interfaces.clipboard import ClipboardBackend
from ..interfaces.shell import ShellBackend
from ..poller import Poller

log = logging.getLogger("desktop-connector")


def run_receiver(
    config: Config,
    crypto: KeyManager,
    headless: bool,
    notifications: NotificationBackend | None = None,
    clipboard: ClipboardBackend | None = None,
    shell: ShellBackend | None = None,
) -> None:
    """Run the receiver loop (with tray or headless)."""
    from ..history import TransferHistory

    conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
    api = ApiClient(conn, crypto)
    history = TransferHistory(config.config_dir)
    notifier = notifications or LinuxNotificationBackend()
    poller = Poller(
        config,
        conn,
        api,
        crypto,
        history,
        clipboard=clipboard,
        notifications=notifier,
        shell=shell,
    )

    # Wire up notifications
    poller.on_file_received(notifier.notify_file_received)

    last_notified = [None]  # "connected", "disconnected", or None (never notified)

    def on_state_change(state):
        if state == ConnectionState.CONNECTED and last_notified[0] != "connected":
            if last_notified[0] == "disconnected":
                notifier.notify_connection_restored()
            last_notified[0] = "connected"
        elif (
            state == ConnectionState.DISCONNECTED
            and last_notified[0] != "disconnected"
        ):
            if last_notified[0] == "connected":
                notifier.notify_connection_lost()
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
        notifications=notifier,
        clipboard=clipboard,
        shell=shell,
    )
    log.info("Starting tray icon. Saving to: %s", config.save_directory)

    def handle_signal(*_):
        log.info("Shutting down...")
        poller.stop()
        tray.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    tray.run()  # Blocks main thread
