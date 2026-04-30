"""
Desktop Connector - Main entry point.

Usage:
    # Normal mode (system tray):
    python -m src.main

    # Headless receiver (no GUI, just polls and saves):
    python -m src.main --headless

    # Headless send a file:
    python -m src.main --headless --send="/path/to/file"
    python -m src.main --headless --send="/path/to/file" --target-device-id="<id>"

    # Custom config directory:
    python -m src.main --config-dir=/path/to/config

    # Pair with a connected device (GUI):
    python -m src.main --pair

    # Pair headless (for testing):
    python -m src.main --headless --pair
"""

import logging
import sys
from pathlib import Path

from .bootstrap.app_version import get_app_version
from .bootstrap.appimage_install_hook import ensure_appimage_integration
from .file_manager_integration import sync_file_manager_targets
from .bootstrap.appimage_migration import migrate_from_apt_pip_if_needed
from .bootstrap.appimage_onboarding import OnboardingResult, run_onboarding_if_needed
from .bootstrap.appimage_relocate import (
    enforce_single_instance,
    relocate_to_canonical_if_needed,
)
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
    # --version short-circuits before dep checks so it works on a
    # minimal AppImage that doesn't yet bundle GTK4 (P.1b).
    if "--version" in sys.argv[1:]:
        print(f"Desktop Connector {get_app_version()}")
        return 0

    # Single-instance enforcement: SIGTERM every other Desktop Connector
    # process on the machine before we set up our own bootstrap. No-op
    # in transient modes (--send / --pair). Catches AppImage at any path,
    # install-from-source layouts, and dev-tree runs.
    enforce_single_instance()

    # Self-install: when the AppImage is run from anywhere other than
    # ~/.local/share/desktop-connector/desktop-connector.AppImage, copy
    # ourselves there, spawn the canonical copy, and exit. No-op outside
    # an AppImage and in transient modes. Override with DC_NO_RELOCATE=1
    # when testing a non-canonical build.
    if relocate_to_canonical_if_needed():
        return 0

    # Headless receivers skip GUI dep checks (no tray, no GTK4 subprocess
    # windows, no tkinter pairing UI). Peek at argv since args haven't
    # been parsed yet — full parse needs args we'd rather check first.
    headless = "--headless" in sys.argv[1:]
    missing = check_dependencies(headless=headless)
    if missing:
        show_missing_deps_dialog(missing)
        return 1

    args = parse_startup_args()
    # Configure logging before any services are constructed so constructor-time
    # log lines aren't dropped or emitted under the default logging config.
    setup_logging(args.verbose, Path(args.config_dir) if args.config_dir else None)

    # H.6+H.7: --scrub-secrets short-circuits before the full startup
    # context (no need to register or pair just to verify storage).
    # Covers config.json fields (auth_token, pairing symkeys) AND the
    # legacy private_key.pem file in one pass.
    if args.scrub_secrets:
        from .config import Config
        from .crypto import KeyManager
        cfg = Config(Path(args.config_dir) if args.config_dir else None)
        result = cfg.scrub_secrets()
        # KeyManager.__init__ runs the PEM-migration check on every
        # construction; .scrub_private_key() catches anything that
        # showed up between Config init and now (e.g. a hand-restored
        # PEM after the H.4 cycle finished). Two-step ensures we
        # cover both code paths in a single CLI invocation.
        crypto = KeyManager(cfg.config_dir, secret_store=cfg.secret_store)
        pem_scrubbed_now = crypto.scrub_private_key()
        pem_scrubbed = crypto.was_pem_migrated or pem_scrubbed_now

        if not result.secure:
            print(
                "Secret storage: plaintext fallback active "
                f"(no OS keyring reachable). config.json: {cfg.config_file}",
                file=sys.stderr,
            )
            return 1
        if result.failed > 0:
            print(
                f"Scrubbed {result.scrubbed} plaintext field(s); "
                f"{result.failed} could not be migrated (keyring "
                "transient). Re-run after fixing the backend.",
                file=sys.stderr,
            )
            return 1

        # Combine config-side counts with the private-key migration
        # signal into one summary line for the operator.
        items: list[str] = []
        if result.scrubbed > 0:
            items.append(f"{result.scrubbed} plaintext field(s)")
        if pem_scrubbed:
            items.append("device private key")
        if items:
            print(
                "Scrubbed " + " and ".join(items) +
                " into the keyring.",
            )
        else:
            print(
                "Secret storage already clean — no plaintext in "
                "config.json or keys/private_key.pem.",
            )
        return 0

    context = build_startup_context(args)

    # H.5: surface the JSON-fallback state to the user at every
    # startup. Tray-mode users also see a clickable menu warning row
    # (tray.py); this stderr line is the CLI / --headless surface.
    if not context.config.is_secret_storage_secure():
        warning = (
            "⚠ Secret Service unavailable — auth_token and pairing keys "
            "stored in plaintext "
            f"{context.config.config_file}. Install gnome-keyring "
            "(GNOME / Zorin / Ubuntu) or kwallet (KDE) and re-launch to "
            "fix. See docs/plans/hardening-plan.md H.5."
        )
        print(warning, file=sys.stderr)
        log.warning("config.secrets.user_warned surface=cli")

    # First-launch GTK4 onboarding (AppImage only, no-op otherwise).
    # Runs before the install hook so an "autostart off" choice can
    # drop a .no-autostart marker the install hook will honour.
    onboarding_result = run_onboarding_if_needed(
        context.config, headless=args.headless
    )

    # Migrate from a classic apt-pip install if one is present (P.4b).
    # Must run before the install hook so the hook then sees the old
    # autostart entry's stale Exec= and rewrites it to point at $APPIMAGE.
    migrate_from_apt_pip_if_needed(
        context.config, context.crypto, context.platform.notifications
    )

    # Drop / refresh AppImage desktop integration. No-op outside an
    # AppImage; runs before register_device so the menu entry exists
    # even if the relay is unreachable on first launch.
    ensure_appimage_integration(context.config)

    # Re-sync per-paired-device file-manager send targets. Picks up
    # pairings/renames/unpairs that landed while the app wasn't
    # running, plus any pre-multi-device "Send to Phone" leftovers.
    # No-op when no launcher (dev tree) is detected.
    try:
        sync_file_manager_targets(context.config)
    except Exception as exc:
        log.warning("file_manager.sync.failed_at_startup: %s", exc)

    unconfigured = False
    if not register_device(context.config, context.api):
        # Soft-fail iff the user just cancelled onboarding from interactive
        # tray mode — per the plan, the tray runs unconfigured and the
        # Settings window can complete setup later. Send/pair/headless
        # callers still hard-fail because they can't proceed without creds.
        soft_fail = (
            onboarding_result is OnboardingResult.CANCELLED
            and not args.headless
            and not args.send
            and not args.pair
        )
        if not soft_fail:
            return 1
        log.info("appimage.onboarding.deferred running tray unregistered")
        unconfigured = True

    if not unconfigured:
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
        return run_send_file(
            context.config,
            context.crypto,
            Path(args.send),
            target_device_id=args.target_device_id,
        )

    run_receiver(
        context.config,
        context.crypto,
        mode == "headless_receive",
        context.platform,
    )
    return 0

if __name__ == "__main__":
    sys.exit(main())
