"""
Desktop Connector - Main entry point.

Usage:
    # Normal mode (system tray):
    python -m src.main

    # Headless receiver (no GUI, just polls and saves):
    python -m src.main --headless

    # Headless send a file:
    python -m src.main --headless --send="/path/to/file"

    # Custom config directory:
    python -m src.main --config-dir=/path/to/config

    # Pair with a phone (GUI):
    python -m src.main --pair

    # Pair headless (for testing):
    python -m src.main --headless --pair
"""

import argparse
import base64


def check_dependencies() -> list[str]:
    """Check all required dependencies. Returns list of missing ones."""
    missing = []

    checks = [
        ("nacl", "PyNaCl", "python3-nacl or: pip install PyNaCl"),
        ("cryptography", "cryptography", "pip install cryptography"),
        ("requests", "requests", "pip install requests"),
        ("PIL", "Pillow", "python3-pil or: pip install Pillow"),
        ("pystray", "pystray", "pip install --user --break-system-packages pystray"),
        ("qrcode", "qrcode", "pip install --user --break-system-packages qrcode"),
    ]

    for module, name, fix in checks:
        try:
            __import__(module)
        except ImportError:
            missing.append((name, fix))

    # Check tkinter
    try:
        import tkinter
    except ImportError:
        missing.append(("tkinter", "sudo apt install python3-tk"))

    # Check PIL.ImageTk
    try:
        from PIL import ImageTk
    except ImportError:
        missing.append(("Pillow-ImageTk", "sudo apt install python3-pil.imagetk"))

    # Check GTK4/libadwaita (for subprocess windows) — test in a subprocess
    # to avoid GTK3/4 conflict with pystray in the main process
    import subprocess as _sp
    result = _sp.run(
        ["python3", "-c", "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw"],
        capture_output=True,
    )
    if result.returncode != 0:
        missing.append(("GTK4/libadwaita", "sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"))

    return missing


def show_missing_deps_dialog(missing: list[tuple[str, str]]) -> None:
    """Show a dialog about missing dependencies with install button."""
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw
        _show_deps_gtk4(missing)
    except Exception:
        try:
            _show_deps_tkinter(missing)
        except Exception:
            # Last resort: print to terminal
            print("\nMissing dependencies:")
            for name, fix in missing:
                print(f"  - {name}: {fix}")
            print("\nRun the installer to fix:")
            print("  curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash\n")


def _show_deps_gtk4(missing):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib
    import subprocess

    app = Adw.Application(application_id="com.desktopconnector.deps")

    def on_activate(app):
        win = Adw.ApplicationWindow(application=app, title="Desktop Connector", default_width=400, default_height=300)
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        toolbar.set_content(box)

        label = Gtk.Label(label="Missing dependencies", xalign=0)
        label.add_css_class("title-3")
        box.append(label)

        for name, fix in missing:
            row = Gtk.Label(label=f"• {name}\n  {fix}", xalign=0, wrap=True)
            row.add_css_class("body")
            box.append(row)

        def on_install(btn):
            subprocess.Popen([
                "gnome-terminal", "--", "bash", "-c",
                "curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash; echo; read -p 'Press Enter to close...'"
            ])
            win.close()

        install_btn = Gtk.Button(label="Install Dependencies")
        install_btn.add_css_class("suggested-action")
        install_btn.connect("clicked", on_install)
        box.append(install_btn)

        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def _show_deps_tkinter(missing):
    import subprocess
    import tkinter as tk

    root = tk.Tk()
    root.title("Desktop Connector — Missing Dependencies")
    root.configure(bg="#1e293b")

    frame = tk.Frame(root, bg="#1e293b", padx=24, pady=24)
    frame.pack()

    tk.Label(frame, text="Missing dependencies", font=("sans-serif", 14, "bold"),
             fg="#f8fafc", bg="#1e293b").pack(anchor=tk.W, pady=(0, 12))

    for name, fix in missing:
        tk.Label(frame, text=f"• {name}: {fix}", font=("sans-serif", 10),
                 fg="#94a3b8", bg="#1e293b", anchor=tk.W, justify=tk.LEFT).pack(anchor=tk.W, pady=2)

    def on_install():
        subprocess.Popen([
            "x-terminal-emulator", "-e", "bash", "-c",
            "curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash; echo; read -p 'Press Enter to close...'"
        ])
        root.destroy()

    tk.Button(frame, text="Install Dependencies", command=on_install,
              font=("sans-serif", 11), bg="#3b82f6", fg="#f8fafc",
              padx=16, pady=6).pack(pady=(16, 0))

    root.mainloop()
import logging
import signal
import sys
import threading
from pathlib import Path

from .api_client import ApiClient
from .config import Config
from .connection import ConnectionManager, ConnectionState
from .crypto import KeyManager
from .notifications import notify_file_received, notify_connection_lost, notify_connection_restored
from .poller import Poller

log = logging.getLogger("desktop-connector")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def register_device(config: Config, crypto: KeyManager, api: ApiClient) -> bool:
    """Register device with server if not already registered."""
    if config.is_registered:
        log.info("Already registered as %s", config.device_id)
        return True

    log.info("Registering device with server at %s...", config.server_url)
    result = api.register(config.server_url)
    if result:
        config.device_id = result["device_id"]
        config.auth_token = result["auth_token"]
        log.info("Registered as %s", config.device_id)
        return True

    log.error("Failed to register with server")
    return False


def run_send_file(config: Config, crypto: KeyManager, filepath: Path) -> int:
    """Send a single file and exit. Returns 0 on success, 1 on failure."""
    if not config.is_registered:
        log.error("Not registered. Run without --send-photo first to register and pair.")
        return 1
    if not config.is_paired:
        log.error("No paired device. Run with --pair first.")
        return 1

    if not filepath.exists():
        log.error("File not found: %s", filepath)
        return 1

    # Get first paired device
    target_id, target_info = config.get_first_paired_device()
    symmetric_key = base64.b64decode(target_info["symmetric_key_b64"])

    conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
    api = ApiClient(conn, crypto)

    # Check connection first
    if not conn.check_connection():
        log.error("Cannot reach server at %s", config.server_url)
        return 1

    tid = api.send_file(filepath, target_id, symmetric_key)
    if tid:
        from .history import TransferHistory
        history = TransferHistory(config.config_dir)
        history.add(filename=filepath.name, display_label=filepath.name,
                     direction="sent", size=filepath.stat().st_size,
                     content_path=str(filepath), transfer_id=tid)
        log.info("File sent successfully")
        return 0
    else:
        log.error("Failed to send file")
        return 1


def run_receiver(config: Config, crypto: KeyManager, headless: bool) -> None:
    """Run the receiver loop (with tray or headless)."""
    from .history import TransferHistory
    conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
    api = ApiClient(conn, crypto)
    history = TransferHistory(config.config_dir)
    poller = Poller(config, conn, api, crypto, history)

    # Wire up notifications
    poller.on_file_received(notify_file_received)

    last_notified = [None]  # "connected", "disconnected", or None (never notified)

    def on_state_change(state):
        if state == ConnectionState.CONNECTED and last_notified[0] != "connected":
            if last_notified[0] == "disconnected":
                notify_connection_restored()
            last_notified[0] = "connected"
        elif state == ConnectionState.DISCONNECTED and last_notified[0] != "disconnected":
            if last_notified[0] == "connected":
                notify_connection_lost()
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
    else:
        from .tray import TrayApp
        tray = TrayApp(conn, poller, api, config, crypto, history, config.save_directory)
        log.info("Starting tray icon. Saving to: %s", config.save_directory)

        def handle_signal(*_):
            log.info("Shutting down...")
            poller.stop()
            tray.stop()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        tray.run()  # Blocks main thread


def main() -> int:
    # Check dependencies before anything else
    missing = check_dependencies()
    if missing:
        show_missing_deps_dialog(missing)
        return 1

    parser = argparse.ArgumentParser(description="Desktop Connector")
    parser.add_argument("--headless", action="store_true", help="Run without GUI")
    parser.add_argument("--send", type=str, help="Send a file and exit")
    parser.add_argument("--pair", action="store_true", help="Start pairing flow")
    parser.add_argument("--config-dir", type=str, help="Config directory path")
    parser.add_argument("--server-url", type=str, help="Override server URL")
    parser.add_argument("--save-dir", type=str, help="Override save directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    setup_logging(args.verbose)

    config = Config(Path(args.config_dir) if args.config_dir else None)
    if args.server_url:
        config.server_url = args.server_url
    if args.save_dir:
        config.save_directory = args.save_dir

    crypto = KeyManager(config.config_dir)
    conn = ConnectionManager(config.server_url, config.device_id or "unregistered", config.auth_token or "none")
    api = ApiClient(conn, crypto)

    # Register if needed
    if not register_device(config, crypto, api):
        return 1

    # Re-create connection with actual credentials
    conn = ConnectionManager(config.server_url, config.device_id, config.auth_token)
    api = ApiClient(conn, crypto)

    # Pair if requested or not yet paired
    if args.pair or not config.is_paired:
        if args.send:
            log.error("Not paired yet. Run with --pair first.")
            return 1
        log.info("Starting pairing flow...")
        if args.headless:
            from .pairing import run_pairing_headless
            if not run_pairing_headless(config, crypto, api):
                return 1
            log.info("Pairing complete!")
        else:
            import subprocess as _sp
            _sp.run([
                sys.executable, "-m", "src.windows", "pairing",
                f"--config-dir={config.config_dir}",
            ], cwd=str(Path(__file__).parent.parent))
            config.reload()
            if not config.is_paired:
                log.error("Pairing cancelled")
                return 1
            log.info("Pairing complete!")

    # Send file mode
    if args.send:
        return run_send_file(config, crypto, Path(args.send))

    # Receiver mode
    run_receiver(config, crypto, args.headless)
    return 0


if __name__ == "__main__":
    sys.exit(main())
