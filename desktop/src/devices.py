"""Connected-device registry for desktop multi-pair state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from .config import Config

log = logging.getLogger(__name__)

SHORT_DEVICE_ID_LEN = 8


class DeviceRegistryError(ValueError):
    """Base error for connected-device registry validation failures."""


class DeviceNotFoundError(DeviceRegistryError):
    """Raised when a caller targets a device that is not paired."""


class DuplicateDeviceNameError(DeviceRegistryError):
    """Raised when a caller tries to save a duplicate device name."""


@dataclass(frozen=True)
class ConnectedDevice:
    device_id: str
    pubkey: str
    name: str
    paired_at: int
    symmetric_key_b64: str | None = None

    @property
    def short_id(self) -> str:
        return short_device_id(self.device_id)


def short_device_id(device_id: str) -> str:
    return device_id[:SHORT_DEVICE_ID_LEN]


def _normalize_name(name: str) -> str:
    return name.strip()


def _name_key(name: str) -> str:
    return _normalize_name(name).casefold()


def next_default_device_name(existing_names: Iterable[str]) -> str:
    """Return Device N, starting at current count + 1.

    The feature requires the initial pairing suggestion to be
    ``Device X`` where X is the number of existing pairs plus one.
    If that candidate is already taken, keep incrementing until the
    suggestion is unique.
    """
    names = list(existing_names)
    taken = {_name_key(name) for name in names if _normalize_name(name)}
    index = len(names) + 1
    while True:
        candidate = f"Device {index}"
        if candidate.casefold() not in taken:
            return candidate
        index += 1


class ConnectedDeviceRegistry:
    """Metadata view over Config.paired_devices.

    Config remains the source of truth for persistence and secret-store
    hydration. This wrapper centralizes sorting, active-device fallback,
    and unique-name policy for the desktop multi-device rollout.
    """

    def __init__(self, config: Config):
        self.config = config

    def list_devices(self, *, normalize_names: bool = True) -> list[ConnectedDevice]:
        if normalize_names:
            self.normalize_duplicate_names()
        return self._load_devices()

    def get(self, device_id: str) -> ConnectedDevice | None:
        for device in self.list_devices(normalize_names=False):
            if device.device_id == device_id:
                return device
        return None

    def get_active_device(self) -> ConnectedDevice | None:
        devices = self.list_devices(normalize_names=False)
        if not devices:
            if self.config.active_device_id is not None:
                self.config.active_device_id = None
            return None

        active_id = self.config.active_device_id
        if active_id:
            for device in devices:
                if device.device_id == active_id:
                    return device
            self.config.active_device_id = None

        return devices[0]

    def mark_active(self, device_id: str, *, reason: str = "") -> ConnectedDevice:
        device = self.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"paired device not found: {device_id}")
        self.config.active_device_id = device_id
        if reason:
            log.info(
                "device.active.changed peer=%s reason=%s",
                short_device_id(device_id),
                reason,
            )
        return device

    def next_default_name(self) -> str:
        return next_default_device_name(device.name for device in self.list_devices())

    def validate_unique_name(
        self,
        name: str,
        *,
        exclude_device_id: str | None = None,
    ) -> str:
        normalized = _normalize_name(name)
        if not normalized:
            raise DeviceRegistryError("device name cannot be empty")

        candidate_key = _name_key(normalized)
        for device in self.list_devices(normalize_names=False):
            if device.device_id == exclude_device_id:
                continue
            if _name_key(device.name) == candidate_key:
                raise DuplicateDeviceNameError(
                    f"device name already exists: {normalized}"
                )
        return normalized

    def rename(self, device_id: str, name: str) -> ConnectedDevice:
        normalized = self.validate_unique_name(
            name,
            exclude_device_id=device_id,
        )
        if not self.config.rename_paired_device(device_id, normalized):
            raise DeviceNotFoundError(f"paired device not found: {device_id}")
        device = self.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"paired device not found: {device_id}")
        return device

    def unpair(self, device_id: str) -> None:
        self.config.remove_paired_device(device_id)
        if self.config.active_device_id == device_id:
            self.config.active_device_id = None

    def normalize_duplicate_names(self) -> bool:
        """Persistently repair duplicate display names.

        Duplicate saves are rejected in normal UI flows, but legacy or
        hand-edited config can still contain duplicates. Keep the first
        device in registry sort order, then rename later duplicates by
        appending a short device id.
        """
        devices = self._load_devices()
        taken: set[str] = set()
        changed = False

        for device in devices:
            name = _normalize_name(device.name)
            if not name:
                name = self._unique_default_name(devices, taken)
            elif _name_key(name) in taken:
                name = self._unique_name(name, device.device_id, taken)

            key = _name_key(name)
            taken.add(key)

            if name != device.name:
                if self.config.rename_paired_device(device.device_id, name):
                    changed = True
                    log.info(
                        "device.name.normalized peer=%s",
                        device.short_id,
                    )

        return changed

    def _unique_name(
        self,
        base_name: str,
        device_id: str,
        taken: set[str],
    ) -> str:
        base = _normalize_name(base_name) or "Device"
        candidate = f"{base} {short_device_id(device_id)}"
        if _name_key(candidate) not in taken:
            return candidate

        # Fall back to space-int suffixes (e.g. "<base> <short> 2") to
        # match the increment style of `next_default_device_name`. We
        # only ever reach this branch when both the base name and the
        # short-id-disambiguated form are taken — vanishingly rare in
        # practice, but consistency keeps the persisted-name shape
        # uniform across both code paths.
        index = 2
        while True:
            numbered = f"{candidate} {index}"
            if _name_key(numbered) not in taken:
                return numbered
            index += 1

    def _unique_default_name(
        self,
        devices: list[ConnectedDevice],
        taken: set[str],
    ) -> str:
        index = len(devices) + 1
        while True:
            candidate = f"Device {index}"
            if _name_key(candidate) not in taken:
                return candidate
            index += 1

    def _load_devices(self) -> list[ConnectedDevice]:
        devices: list[ConnectedDevice] = []
        for device_id, info in self.config.paired_devices.items():
            if not isinstance(info, dict):
                continue
            devices.append(
                ConnectedDevice(
                    device_id=device_id,
                    pubkey=str(info.get("pubkey", "")),
                    name=str(info.get("name", "")),
                    paired_at=_coerce_int(info.get("paired_at")),
                    symmetric_key_b64=_optional_str(info.get("symmetric_key_b64")),
                )
            )
        return sorted(
            devices,
            key=lambda device: (
                -device.paired_at,
                device.name.casefold(),
                device.device_id,
            ),
        )


def resolve_target_device(
    config: Config,
    *,
    target_device_id: str | None = None,
) -> ConnectedDevice:
    """Resolve the paired device a non-GTK send should target.

    Explicit CLI ids win. Without an explicit id, fall back to the
    active device, then the registry default device when no active
    device is set yet.
    """
    registry = ConnectedDeviceRegistry(config)
    requested_id = (target_device_id or "").strip()
    if requested_id:
        device = registry.get(requested_id)
        if device is None:
            raise DeviceNotFoundError(
                f"target device is not paired: {requested_id}"
            )
        return device

    device = registry.get_active_device()
    if device is None:
        raise DeviceNotFoundError("no paired device")
    return device


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
