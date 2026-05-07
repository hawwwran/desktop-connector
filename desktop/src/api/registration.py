"""Device registration with the relay."""

import logging

import requests

from .outcomes import DeviceRegistrationResult

log = logging.getLogger(__name__)


class RegistrationMixin:
    def register(self, server_url: str, device_type: str = "desktop") -> dict | None:
        """Register this device with the server. Returns {device_id, auth_token} or None."""
        result = self.register_with_status(server_url, device_type)
        if result is not None and result.is_successful:
            return result.body
        return None

    def register_with_status(
        self,
        server_url: str,
        device_type: str = "desktop",
    ) -> DeviceRegistrationResult | None:
        """Register this device and expose the HTTP status for recovery decisions."""
        try:
            resp = requests.post(
                f"{server_url}/api/devices/register",
                json={
                    "public_key": self.crypto.get_public_key_b64(),
                    "device_type": device_type,
                },
                timeout=10,
            )
            body = None
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                pass
            if resp.status_code in (200, 201):
                return DeviceRegistrationResult(resp.status_code, body)
            log.error("Registration failed: %d %s", resp.status_code, resp.text)
            return DeviceRegistrationResult(resp.status_code, body)
        except requests.RequestException as e:
            log.error("Registration request failed: %s", e)
        return None
