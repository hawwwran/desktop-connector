"""Argument parsing and startup mode resolution."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StartupArgs:
    headless: bool
    send: str | None
    pair: bool
    config_dir: str | None
    server_url: str | None
    save_dir: str | None
    verbose: bool


StartupMode = Literal["send_file", "headless_receive", "tray_receive"]


def parse_startup_args() -> StartupArgs:
    """Parse CLI arguments for desktop startup."""
    parser = argparse.ArgumentParser(description="Desktop Connector")
    parser.add_argument("--headless", action="store_true", help="Run without GUI")
    parser.add_argument("--send", type=str, help="Send a file and exit")
    parser.add_argument("--pair", action="store_true", help="Start pairing flow")
    parser.add_argument("--config-dir", type=str, help="Config directory path")
    parser.add_argument("--server-url", type=str, help="Override server URL")
    parser.add_argument("--save-dir", type=str, help="Override save directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parsed = parser.parse_args()

    return StartupArgs(
        headless=parsed.headless,
        send=parsed.send,
        pair=parsed.pair,
        config_dir=parsed.config_dir,
        server_url=parsed.server_url,
        save_dir=parsed.save_dir,
        verbose=parsed.verbose,
    )


def resolve_startup_mode(args: StartupArgs) -> StartupMode:
    """Resolve the final startup mode (what to do after any pairing step)."""
    if args.send:
        return "send_file"
    if args.headless:
        return "headless_receive"
    return "tray_receive"
