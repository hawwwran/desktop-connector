"""Shared startup context wiring for desktop bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..api_client import ApiClient
from ..config import Config
from ..connection import ConnectionManager
from ..crypto import KeyManager
from ..platform import DesktopPlatform
from ..platform.compose import compose_desktop_platform
from .args import StartupArgs


@dataclass
class StartupContext:
    args: StartupArgs
    config: Config
    crypto: KeyManager
    api: ApiClient
    platform: DesktopPlatform


class IdentityLockedInKeyring(Exception):
    """The install's identity lives in the OS keyring and the keyring is
    locked or unreachable, so startup has no usable credentials."""


def build_startup_context(args: StartupArgs) -> StartupContext:
    """Create shared startup services from parsed CLI args."""
    config = Config(Path(args.config_dir) if args.config_dir else None)
    if config.identity_locked_in_keyring:
        # Raised before KeyManager runs: in fallback mode it would
        # generate a fresh private key file for an identity nobody has.
        raise IdentityLockedInKeyring()
    if args.server_url:
        config.server_url = args.server_url
    if args.save_dir:
        config.save_directory = args.save_dir

    # H.7: pass the same store Config picked so the private key
    # rides on the same backend as auth_token + pairing symkeys.
    crypto = KeyManager(config.config_dir, secret_store=config.secret_store)
    conn = ConnectionManager(
        config.server_url,
        config.device_id or "unregistered",
        config.auth_token or "none",
    )
    api = ApiClient(conn, crypto)
    platform = compose_desktop_platform()

    return StartupContext(args=args, config=config, crypto=crypto, api=api, platform=platform)


def stored_identity_matches_key(config: Config, crypto: KeyManager) -> bool:
    """False when the stored device_id is not the one this device's key
    derives to.

    The relay issues ``device_id`` as a hash of the public key, and every
    pairing is made against that key. A stored id that belongs to some
    other key authenticates fine but is paired with nobody, so sends
    under it are accepted nowhere. An unregistered install has nothing
    to compare and passes.
    """
    stored = config.device_id
    return stored is None or stored == crypto.get_device_id()


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
