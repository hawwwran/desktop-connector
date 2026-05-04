#!/usr/bin/env python3
"""
GTK4/libadwaita windows — run as a separate process to avoid GTK3/4 conflict with pystray.

The window code itself lives in sibling modules (``windows_send.py``,
``windows_settings.py``, …); this file is the CLI dispatcher only.

Usage:
    python3 -m src.windows send-files --config-dir=~/.config/desktop-connector
    python3 -m src.windows settings --config-dir=~/.config/desktop-connector
    python3 -m src.windows history --config-dir=~/.config/desktop-connector
"""

import argparse
from pathlib import Path

from .windows_common import _setup_subprocess_logging
from .windows_find_phone import show_find_phone, show_locate_alert
from .windows_history import show_history
from .windows_onboarding import show_onboarding, show_secret_storage_warning
from .windows_pairing import show_pairing
from .windows_send import show_send_files
from .windows_settings import show_settings
from .windows_vault_browser import show_vault_browser
from .windows_vault import (
    show_vault_main,
    show_vault_onboard,
    show_vault_passphrase_generator,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "window",
        choices=[
            "send-files", "settings", "history", "pairing",
            "find-phone", "locate-alert", "onboarding",
            "secret-storage-warning",
            "vault-main", "vault-browser", "vault-onboard", "vault-passphrase-generator",
        ],
    )
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--sender-name", default="")
    args = parser.parse_args()

    config_dir = Path(args.config_dir)
    _setup_subprocess_logging(config_dir)

    if args.window == "send-files":
        show_send_files(config_dir)
    elif args.window == "settings":
        show_settings(config_dir)
    elif args.window == "history":
        show_history(config_dir)
    elif args.window == "pairing":
        show_pairing(config_dir)
    elif args.window == "find-phone":
        show_find_phone(config_dir)
    elif args.window == "locate-alert":
        show_locate_alert(config_dir, sender_name=args.sender_name or "another device")
    elif args.window == "onboarding":
        show_onboarding(config_dir)
    elif args.window == "secret-storage-warning":
        show_secret_storage_warning(config_dir)
    elif args.window == "vault-main":
        show_vault_main(config_dir)
    elif args.window == "vault-browser":
        show_vault_browser(config_dir)
    elif args.window == "vault-onboard":
        show_vault_onboard(config_dir)
    elif args.window == "vault-passphrase-generator":
        show_vault_passphrase_generator(config_dir)


if __name__ == "__main__":
    main()
