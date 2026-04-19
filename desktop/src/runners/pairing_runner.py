"""Pairing startup runner."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from ..api_client import ApiClient
from ..config import Config
from ..crypto import KeyManager

log = logging.getLogger("desktop-connector")


def run_pairing_flow(
    config: Config,
    crypto: KeyManager,
    api: ApiClient,
    *,
    headless: bool,
    send: str | None,
) -> int:
    """Execute pairing flow, preserving existing CLI behavior."""
    if send:
        log.error("Not paired yet. Run with --pair first.")
        return 1

    log.info("Starting pairing flow...")

    if headless:
        from ..pairing import run_pairing_headless

        if not run_pairing_headless(config, crypto, api):
            return 1
        log.info("Pairing complete!")
        return 0

    subprocess.run(
        [
            sys.executable,
            "-m",
            "src.windows",
            "pairing",
            f"--config-dir={config.config_dir}",
        ],
        cwd=str(Path(__file__).parent.parent.parent),
    )
    config.reload()
    if not config.is_paired:
        log.error("Pairing cancelled")
        return 1

    log.info("Pairing complete!")
    return 0
