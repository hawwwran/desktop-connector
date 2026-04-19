"""Shared startup context wiring for desktop bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..api_client import ApiClient
from ..config import Config
from ..connection import ConnectionManager
from ..crypto import KeyManager
from ..interfaces.backends import DesktopBackends
from ..platform.linux.compose import compose_linux_backends
from .args import StartupArgs


@dataclass
class StartupContext:
    args: StartupArgs
    config: Config
    crypto: KeyManager
    api: ApiClient
    backends: DesktopBackends


def build_startup_context(args: StartupArgs) -> StartupContext:
    """Create shared startup services from parsed CLI args."""
    config = Config(Path(args.config_dir) if args.config_dir else None)
    if args.server_url:
        config.server_url = args.server_url
    if args.save_dir:
        config.save_directory = args.save_dir

    crypto = KeyManager(config.config_dir)
    conn = ConnectionManager(
        config.server_url,
        config.device_id or "unregistered",
        config.auth_token or "none",
    )
    api = ApiClient(conn, crypto)
    backends = compose_linux_backends()

    return StartupContext(args=args, config=config, crypto=crypto, api=api, backends=backends)


def rebuild_authenticated_api(context: StartupContext) -> None:
    """Recreate API client with freshly registered credentials.

    Mutates ``context.api`` in place; callers rely on this because the
    initial context is built with placeholder credentials before
    ``register_device`` runs.
    """
    conn = ConnectionManager(
        context.config.server_url,
        context.config.device_id,
        context.config.auth_token,
    )
    context.api = ApiClient(conn, context.crypto)
