"""Secret-storage abstraction for desktop at-rest credentials.

The desktop client's two pieces of secret material — the server
``auth_token`` and the per-pairing ``symmetric_key_b64`` — currently
live in plain JSON inside ``config.json``. Moving them out to the OS
secret store (libsecret on GNOME-family / KWallet on KDE) is the
substance of hardening-plan.md.

This module owns the seam in the code that future backends slot
into. H.2 introduces the abstraction without changing on-disk
behaviour: the only implementation here is :class:`JsonFallbackStore`,
which keeps secrets in the same JSON dict ``Config`` already
manages, in the same fields the previous code used. H.3 will add
``SecretServiceStore`` against libsecret; the call sites in
``config.py`` won't need to change.

Canonical secret keys are defined here so all backends agree on
the lookup namespace:

- ``SECRET_KEY_AUTH_TOKEN`` — server bearer token
- ``pairing_symkey_key(device_id)`` — per-pairing symmetric key
"""

from __future__ import annotations

from typing import Callable, Protocol


SECRET_KEY_AUTH_TOKEN = "auth_token"
_PAIRING_SYMKEY_PREFIX = "pairing_symkey:"


def pairing_symkey_key(device_id: str) -> str:
    """Canonical secret-store key for a pairing symmetric key."""
    return f"{_PAIRING_SYMKEY_PREFIX}{device_id}"


def parse_pairing_symkey_key(key: str) -> str | None:
    """Inverse of :func:`pairing_symkey_key`. Returns the device id
    if ``key`` is a pairing-symkey lookup, else ``None``."""
    if key.startswith(_PAIRING_SYMKEY_PREFIX):
        return key[len(_PAIRING_SYMKEY_PREFIX):]
    return None


class SecretStore(Protocol):
    """Tiny key-value contract for at-rest secrets.

    Backends commit on ``set`` and ``delete`` — once those calls
    return, the change is durable. ``is_secure`` lets callers
    detect a fallback mode that should be opt-in only (relevant
    for headless deployments without a Secret Service).
    """

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def is_secure(self) -> bool: ...


class JsonFallbackStore:
    """Stores secrets inside the existing config.json dict.

    Byte-equivalent to pre-H.2 on-disk state: ``auth_token`` lives
    at the top of ``_data``; per-pairing symmetric keys live at
    ``_data["paired_devices"][device_id]["symmetric_key_b64"]``,
    alongside the non-secret metadata (``pubkey``, ``name``,
    ``paired_at``).

    Mutations call back into ``save_fn`` so durability is the
    backend's responsibility — callers can treat ``set``/``delete``
    as commits without worrying about which backend they have.
    ``is_secure() → False`` makes the insecure fallback explicit
    so H.5 can refuse to use it on headless systems unless the
    operator opts in.
    """

    def __init__(self, data: dict, save_fn: Callable[[], None]) -> None:
        self._data = data
        self._save = save_fn

    def is_secure(self) -> bool:
        return False

    def get(self, key: str) -> str | None:
        if key == SECRET_KEY_AUTH_TOKEN:
            value = self._data.get("auth_token")
            return value if isinstance(value, str) else None
        device_id = parse_pairing_symkey_key(key)
        if device_id is not None:
            paired = self._data.get("paired_devices", {})
            entry = paired.get(device_id, {})
            value = entry.get("symmetric_key_b64")
            return value if isinstance(value, str) else None
        return None

    def set(self, key: str, value: str) -> None:
        if key == SECRET_KEY_AUTH_TOKEN:
            self._data["auth_token"] = value
            self._save()
            return
        device_id = parse_pairing_symkey_key(key)
        if device_id is not None:
            paired = self._data.setdefault("paired_devices", {})
            entry = paired.setdefault(device_id, {})
            entry["symmetric_key_b64"] = value
            self._save()
            return
        raise ValueError(f"unknown secret key: {key!r}")

    def delete(self, key: str) -> None:
        if key == SECRET_KEY_AUTH_TOKEN:
            if "auth_token" in self._data:
                del self._data["auth_token"]
                self._save()
            return
        device_id = parse_pairing_symkey_key(key)
        if device_id is not None:
            paired = self._data.get("paired_devices", {})
            entry = paired.get(device_id)
            if entry is not None and "symmetric_key_b64" in entry:
                del entry["symmetric_key_b64"]
                self._save()
            return
        raise ValueError(f"unknown secret key: {key!r}")
