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

import logging
import sys
from pathlib import Path

from .bootstrap.args import parse_startup_args, resolve_startup_mode
from .bootstrap.startup_context import build_startup_context, rebuild_authenticated_api
from .bootstrap.dependency_check import check_dependencies, show_missing_deps_dialog
from .bootstrap.logging_setup import setup_logging
from .runners.pairing_runner import run_pairing_flow
from .runners.registration_runner import register_device
from .runners.receiver_runner import run_receiver
from .runners.send_runner import run_send_file

log = logging.getLogger("desktop-connector")

def main() -> int:
    # Check dependencies before anything else
    missing = check_dependencies()
    if missing:
        show_missing_deps_dialog(missing)
        return 1

    args = parse_startup_args()
    # Configure logging before any services are constructed so constructor-time
    # log lines aren't dropped or emitted under the default logging config.
    setup_logging(args.verbose, Path(args.config_dir) if args.config_dir else None)

    context = build_startup_context(args)

    if not register_device(context.config, context.api):
        return 1

    rebuild_authenticated_api(context)

    if args.pair or not context.config.is_paired:
        if args.send:
            log.error("Not paired yet. Run with --pair first.")
            return 1
        if run_pairing_flow(
            context.config,
            context.crypto,
            context.api,
            headless=args.headless,
        ) != 0:
            return 1

    mode = resolve_startup_mode(args)
    if mode == "send_file":
        return run_send_file(context.config, context.crypto, Path(args.send))

    run_receiver(
        context.config,
        context.crypto,
        mode == "headless_receive",
        notifications=context.backends.notifications,
        clipboard=context.backends.clipboard,
        shell=context.backends.shell,
    )
    return 0

if __name__ == "__main__":
    sys.exit(main())
