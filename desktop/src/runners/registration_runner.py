"""Registration bootstrap runner."""

from __future__ import annotations

import logging

from ..api_client import ApiClient
from ..config import Config

log = logging.getLogger("desktop-connector")


def register_device(config: Config, api: ApiClient) -> bool:
    """Register device with server if not already registered."""
    if config.identity_locked_in_keyring:
        # The fallback store looks unregistered here only because the
        # keyring holding the real identity is locked. Registering would
        # mint a second device that the phone isn't paired with.
        log.error(
            "startup.register.refused reason=keyring_unreachable "
            "(identity lives in the OS keyring; unlock it and retry)"
        )
        return False

    if config.is_registered:
        log.info("Already registered as %s", config.device_id)
        return True

    log.info("Registering device with server at %s...", config.server_url)
    result = api.register_with_status(config.server_url)
    if result is not None and result.is_successful and result.body:
        config.device_id = result.body["device_id"]
        config.auth_token = result.body["auth_token"]
        log.info("Registered as %s", config.device_id)
        return True

    if result is not None and result.status_code == 409:
        log.warning("Registration conflict; rotating desktop identity and retrying")
        config.wipe_credentials("full")
        api.crypto.reset_keys()
        result = api.register_with_status(config.server_url)
        if result is not None and result.is_successful and result.body:
            config.device_id = result.body["device_id"]
            config.auth_token = result.body["auth_token"]
            log.info("Registered as %s after identity rotation", config.device_id)
            return True

    log.error("Failed to register with server")
    return False
