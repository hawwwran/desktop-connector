"""Registration bootstrap runner."""

from __future__ import annotations

import logging

from ..api_client import ApiClient
from ..config import Config
from ..crypto import KeyManager

log = logging.getLogger("desktop-connector")


def register_device(config: Config, _crypto: KeyManager, api: ApiClient) -> bool:
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
