"""Peer (paired-device) lookups used by transfer + fasttrack handlers."""

import base64
import logging

log = logging.getLogger(__name__)


class DeviceHelpersMixin:
    def _mark_active_device(self, device_id: str, *, reason: str) -> None:
        try:
            self.config.active_device_id = device_id
            log.info(
                "device.active.changed peer=%s reason=%s",
                device_id[:8],
                reason,
            )
        except Exception:
            log.debug(
                "device.active.update_failed peer=%s reason=%s",
                device_id[:8],
                reason,
                exc_info=True,
            )

    def _lookup_device_name(self, device_id: str) -> str | None:
        try:
            entry = self.config.paired_devices.get(device_id)
        except Exception:
            return None
        if not isinstance(entry, dict):
            return None
        name = entry.get("name", "")
        return name if isinstance(name, str) and name.strip() else None

    def _resolve_symmetric_key(self, device_id: str) -> bytes | None:
        try:
            b64 = self.config.get_pairing_symkey(device_id)
        except Exception:
            return None
        if not b64:
            return None
        try:
            return base64.b64decode(b64)
        except Exception:
            return None
