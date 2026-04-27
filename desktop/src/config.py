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

RECEIVE_ACTION_OPEN = "open"
RECEIVE_ACTION_COPY = "copy"
RECEIVE_ACTION_NONE = "none"

RECEIVE_ACTION_KEY_URL_OPEN = "url.open"
RECEIVE_ACTION_KEY_URL_COPY = "url.copy"
RECEIVE_ACTION_KEY_TEXT_COPY = "text.copy"
RECEIVE_ACTION_KEY_IMAGE_OPEN = "image.open"
RECEIVE_ACTION_KEY_VIDEO_OPEN = "video.open"
RECEIVE_ACTION_KEY_DOCUMENT_OPEN = "document.open"

RECEIVE_ACTION_LIMIT_BATCH = "batch"
RECEIVE_ACTION_LIMIT_MINUTE = "minute"
RECEIVE_ACTION_LIMIT_MAX = 999

RECEIVE_KIND_URL = "url"
RECEIVE_KIND_TEXT = "text"
RECEIVE_KIND_IMAGE = "image"
RECEIVE_KIND_VIDEO = "video"
RECEIVE_KIND_DOCUMENT = "document"

DEFAULT_RECEIVE_ACTIONS = {
    RECEIVE_KIND_URL: RECEIVE_ACTION_OPEN,
    RECEIVE_KIND_TEXT: RECEIVE_ACTION_COPY,
    RECEIVE_KIND_IMAGE: RECEIVE_ACTION_NONE,
    RECEIVE_KIND_VIDEO: RECEIVE_ACTION_NONE,
    RECEIVE_KIND_DOCUMENT: RECEIVE_ACTION_NONE,
}

DEFAULT_RECEIVE_ACTION_LIMITS = {
    RECEIVE_ACTION_KEY_URL_OPEN: {
        RECEIVE_ACTION_LIMIT_BATCH: 1,
        RECEIVE_ACTION_LIMIT_MINUTE: 5,
    },
    RECEIVE_ACTION_KEY_URL_COPY: {
        RECEIVE_ACTION_LIMIT_BATCH: 1,
        RECEIVE_ACTION_LIMIT_MINUTE: 10,
    },
    RECEIVE_ACTION_KEY_TEXT_COPY: {
        RECEIVE_ACTION_LIMIT_BATCH: 1,
        RECEIVE_ACTION_LIMIT_MINUTE: 10,
    },
    RECEIVE_ACTION_KEY_IMAGE_OPEN: {
        RECEIVE_ACTION_LIMIT_BATCH: 1,
        RECEIVE_ACTION_LIMIT_MINUTE: 5,
    },
    RECEIVE_ACTION_KEY_VIDEO_OPEN: {
        RECEIVE_ACTION_LIMIT_BATCH: 1,
        RECEIVE_ACTION_LIMIT_MINUTE: 2,
    },
    RECEIVE_ACTION_KEY_DOCUMENT_OPEN: {
        RECEIVE_ACTION_LIMIT_BATCH: 1,
        RECEIVE_ACTION_LIMIT_MINUTE: 5,
    },
}

_RECEIVE_ACTIONS_BY_KIND = {
    RECEIVE_KIND_URL: {
        RECEIVE_ACTION_OPEN,
        RECEIVE_ACTION_COPY,
        RECEIVE_ACTION_NONE,
    },
    RECEIVE_KIND_TEXT: {RECEIVE_ACTION_COPY, RECEIVE_ACTION_NONE},
    RECEIVE_KIND_IMAGE: {RECEIVE_ACTION_OPEN, RECEIVE_ACTION_NONE},
    RECEIVE_KIND_VIDEO: {RECEIVE_ACTION_OPEN, RECEIVE_ACTION_NONE},
    RECEIVE_KIND_DOCUMENT: {RECEIVE_ACTION_OPEN, RECEIVE_ACTION_NONE},
}


def _default_receive_action_limits() -> dict[str, dict[str, int]]:
    return {
        action_key: dict(limits)
        for action_key, limits in DEFAULT_RECEIVE_ACTION_LIMITS.items()
    }


def allowed_receive_actions(kind: str) -> set[str]:
    """Return valid receive-action values for an item kind."""
    return set(_RECEIVE_ACTIONS_BY_KIND.get(kind, set()))


def _normalize_receive_actions(value: object) -> dict[str, str]:
    actions = dict(DEFAULT_RECEIVE_ACTIONS)
    if not isinstance(value, dict):
        return actions

    for kind, action in value.items():
        if kind in actions and action in allowed_receive_actions(kind):
            actions[kind] = action
    return actions


def _normalize_receive_action_limit_value(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < 0:
        return default
    return min(value, RECEIVE_ACTION_LIMIT_MAX)


def _normalize_receive_action_limits(value: object) -> dict[str, dict[str, int]]:
    limits = _default_receive_action_limits()
    if not isinstance(value, dict):
        return limits

    for action_key, stored_limits in value.items():
        if action_key not in limits or not isinstance(stored_limits, dict):
            continue

        normalized = dict(limits[action_key])
        for limit_name in (RECEIVE_ACTION_LIMIT_BATCH, RECEIVE_ACTION_LIMIT_MINUTE):
            if limit_name in stored_limits:
                normalized[limit_name] = _normalize_receive_action_limit_value(
                    stored_limits[limit_name],
                    limits[action_key][limit_name],
                )
        limits[action_key] = normalized

    return limits


class Config:
    """Manages persistent configuration."""

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.json"
        self._data = self._load()
        self._migrate_receive_actions()
        self._migrate_receive_action_limits()

    def _load(self) -> dict:
        if self.config_file.exists():
            with open(self.config_file) as f:
                return json.load(f)
        return {}

    def save(self) -> None:
        with open(self.config_file, "w") as f:
            json.dump(self._data, f, indent=2)

    def _migrate_receive_actions(self) -> None:
        stored = self._data.get("receive_actions")
        if stored is None:
            actions = dict(DEFAULT_RECEIVE_ACTIONS)
            if self._data.get("auto_open_links", True) is False:
                actions[RECEIVE_KIND_URL] = RECEIVE_ACTION_NONE
            self._data["receive_actions"] = actions
            self.save()
            return

        normalized = _normalize_receive_actions(stored)
        if stored != normalized:
            self._data["receive_actions"] = normalized
            self.save()

    def _migrate_receive_action_limits(self) -> None:
        stored = self._data.get("receive_action_limits")
        normalized = _normalize_receive_action_limits(stored)
        if stored != normalized:
            self._data["receive_action_limits"] = normalized
            self.save()

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
        self._migrate_receive_actions()
        self._migrate_receive_action_limits()

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

    def wipe_credentials(self, scope: str) -> None:
        """
        Drop credentials so the next startup re-registers and/or re-pairs.

        scope:
          * 'pairing_only' — clear only paired_devices; keep device_id + auth_token.
            Matches the 403 "Devices are not paired" recovery: auth still works,
            just the server-side pairings row is gone.
          * 'full' — also clear device_id + auth_token. Matches the 401
            "Invalid credentials" recovery: server's DB no longer recognises us
            (either the row was lost or a restored backup reverted our token).
            Caller should also reset_keys() on the KeyManager so a fresh
            public key generates a fresh device_id on next register.
        """
        if scope not in ("pairing_only", "full"):
            raise ValueError(f"unknown wipe scope: {scope}")
        self._data.pop("paired_devices", None)
        if scope == "full":
            self._data.pop("auth_token", None)
            self._data.pop("device_id", None)
        self.save()

    @property
    def auto_open_links(self) -> bool:
        return self._data.get("auto_open_links", True)

    @auto_open_links.setter
    def auto_open_links(self, value: bool) -> None:
        self._data["auto_open_links"] = value
        self.save()

    @property
    def receive_actions(self) -> dict[str, str]:
        return _normalize_receive_actions(self._data.get("receive_actions"))

    @receive_actions.setter
    def receive_actions(self, value: dict[str, str]) -> None:
        self._data["receive_actions"] = _normalize_receive_actions(value)
        self.save()

    def get_receive_action(self, kind: str) -> str:
        return self.receive_actions.get(
            kind,
            DEFAULT_RECEIVE_ACTIONS.get(kind, RECEIVE_ACTION_NONE),
        )

    def set_receive_action(self, kind: str, action: str) -> None:
        actions = self.receive_actions
        if kind in actions and action in allowed_receive_actions(kind):
            actions[kind] = action
        self.receive_actions = actions

    @property
    def receive_action_limits(self) -> dict[str, dict[str, int]]:
        return _normalize_receive_action_limits(
            self._data.get("receive_action_limits")
        )

    @receive_action_limits.setter
    def receive_action_limits(self, value: dict[str, dict[str, int]]) -> None:
        self._data["receive_action_limits"] = _normalize_receive_action_limits(value)
        self.save()

    def get_receive_action_limits(self, action_key: str) -> dict[str, int]:
        limits = self.receive_action_limits.get(action_key)
        if limits is None:
            return {RECEIVE_ACTION_LIMIT_BATCH: 0, RECEIVE_ACTION_LIMIT_MINUTE: 0}
        return dict(limits)

    def set_receive_action_limit(self, action_key: str, limit_name: str,
                                 value: int) -> None:
        limits = self.receive_action_limits
        if action_key in limits and limit_name in (
            RECEIVE_ACTION_LIMIT_BATCH,
            RECEIVE_ACTION_LIMIT_MINUTE,
        ):
            limits[action_key][limit_name] = value
        self.receive_action_limits = limits

    def reset_receive_action_limits(self) -> None:
        self.receive_action_limits = _default_receive_action_limits()

    @property
    def allow_logging(self) -> bool:
        return self._data.get("allow_logging", False)

    @allow_logging.setter
    def allow_logging(self, value: bool) -> None:
        self._data["allow_logging"] = value
        self.save()

    @property
    def appimage_install_hook_done(self) -> bool:
        return bool(self._data.get("appimage_install_hook_done", False))

    @appimage_install_hook_done.setter
    def appimage_install_hook_done(self, value: bool) -> None:
        self._data["appimage_install_hook_done"] = bool(value)
        self.save()

    @property
    def is_registered(self) -> bool:
        return self.auth_token is not None and self.device_id is not None

    @property
    def is_paired(self) -> bool:
        return len(self.paired_devices) > 0
