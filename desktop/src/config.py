"""
Configuration management for Desktop Connector.
"""

import json
import os
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "desktop-connector"
DEFAULT_SAVE_DIR = Path.home() / "Desktop-Connector"
DEFAULT_SERVER_URL = "http://localhost:4441"
DEFAULT_POLL_INTERVAL = 30  # seconds when idle
FAST_POLL_INTERVAL = 5      # seconds after a transfer is found
FAST_POLL_DURATION = 120    # seconds to stay in fast-poll mode


class Config:
    """Manages persistent configuration."""

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.json"
        self._data = self._load()

    def _load(self) -> dict:
        if self.config_file.exists():
            with open(self.config_file) as f:
                return json.load(f)
        return {}

    def save(self) -> None:
        with open(self.config_file, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def server_url(self) -> str:
        return self._data.get("server_url", DEFAULT_SERVER_URL)

    @server_url.setter
    def server_url(self, value: str) -> None:
        self._data["server_url"] = value.rstrip("/")
        self.save()

    @property
    def save_directory(self) -> Path:
        p = Path(self._data.get("save_directory", str(DEFAULT_SAVE_DIR)))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @save_directory.setter
    def save_directory(self, value: str | Path) -> None:
        self._data["save_directory"] = str(value)
        self.save()

    @property
    def device_name(self) -> str:
        return self._data.get("device_name", os.uname().nodename)

    @device_name.setter
    def device_name(self, value: str) -> None:
        self._data["device_name"] = value
        self.save()

    @property
    def auth_token(self) -> str | None:
        return self._data.get("auth_token")

    @auth_token.setter
    def auth_token(self, value: str) -> None:
        self._data["auth_token"] = value
        self.save()

    @property
    def device_id(self) -> str | None:
        return self._data.get("device_id")

    @device_id.setter
    def device_id(self, value: str) -> None:
        self._data["device_id"] = value
        self.save()

    def reload(self) -> None:
        """Reload config from disk (picks up changes from subprocesses)."""
        self._data = self._load()

    @property
    def paired_devices(self) -> dict:
        """Returns dict of {device_id: {pubkey, symmetric_key_b64, name, paired_at}}."""
        self.reload()
        return self._data.get("paired_devices", {})

    def add_paired_device(self, device_id: str, pubkey: str, symmetric_key_b64: str, name: str = "") -> None:
        if "paired_devices" not in self._data:
            self._data["paired_devices"] = {}
        self._data["paired_devices"][device_id] = {
            "pubkey": pubkey,
            "symmetric_key_b64": symmetric_key_b64,
            "name": name,
            "paired_at": int(__import__("time").time()),
        }
        self.save()

    def get_first_paired_device(self) -> tuple[str, dict] | None:
        """Returns (device_id, info) of the first paired device, or None."""
        devices = self.paired_devices
        if devices:
            did = next(iter(devices))
            return did, devices[did]
        return None

    @property
    def auto_open_links(self) -> bool:
        return self._data.get("auto_open_links", True)

    @auto_open_links.setter
    def auto_open_links(self, value: bool) -> None:
        self._data["auto_open_links"] = value
        self.save()

    @property
    def is_registered(self) -> bool:
        return self.auth_token is not None and self.device_id is not None

    @property
    def is_paired(self) -> bool:
        return len(self.paired_devices) > 0
