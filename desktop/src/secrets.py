"""Secret-storage abstraction for desktop at-rest credentials.

The desktop client's two pieces of secret material — the server
``auth_token`` and the per-pairing ``symmetric_key_b64`` — currently
live in plain JSON inside ``config.json``. Moving them out to the OS
secret store (libsecret on GNOME-family / KWallet on KDE) is the
substance of hardening-plan.md.

This module owns the seam in the code that future backends slot
into. H.2 introduced the abstraction without changing on-disk
behaviour: :class:`JsonFallbackStore` keeps secrets in the same
JSON dict ``Config`` already manages, in the same fields the previous
code used. H.3 adds :class:`SecretServiceStore` against the OS
keyring via the ``keyring`` package; the call sites in ``config.py``
don't need to change.

Backend choice (H.3): the ``keyring`` package wins over a direct
``gi.repository.Secret`` integration because it's a tiny pure-Python
dep (~150 KiB with ``jeepney``), works as-is in the AppImage bundle
without extra typelib wiring, and abstracts cleanly across libsecret
(GNOME) / KWallet (KDE) / fail-noisily (headless without a session
bus). ``secretstorage`` was a close second; ``gi.repository.Secret``
was rejected because the AppImage's bundled GTK4 stack does not
pull in libsecret-1 by default and we'd have to add it explicitly
just to talk to the same backend ``keyring`` reaches over D-Bus.

Canonical secret keys are defined here so all backends agree on
the lookup namespace:

- ``SECRET_KEY_AUTH_TOKEN`` — server bearer token
- ``pairing_symkey_key(device_id)`` — per-pairing symmetric key
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)


SECRET_KEY_AUTH_TOKEN = "auth_token"
_PAIRING_SYMKEY_PREFIX = "pairing_symkey:"

# Service name used as the libsecret / Secret Service collection
# attribute. Shows up in seahorse / kwalletmanager next to each
# entry, alongside the per-secret key (``auth_token`` /
# ``pairing_symkey:<device_id>``).
SERVICE_NAME = "desktop-connector"


class SecretServiceUnavailable(Exception):
    """Raised when the OS Secret Service can't be reached.

    Concrete causes include: ``keyring`` package not installed
    (dev tree), no D-Bus session bus (typical on headless
    servers), the keyring daemon isn't running, the active backend
    raised at probe time. Callers — H.4 (legacy migration), H.5
    (headless opt-in) — branch on this to either fall back
    explicitly or surface a clear error.
    """


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


class SecretServiceStore:
    """OS Secret Service / libsecret backend.

    Uses the ``keyring`` package so we don't depend on any single
    backend (libsecret on GNOME, KWallet on KDE, etc.). Each secret
    is stored against ``SERVICE_NAME`` plus the canonical key as
    the keyring "username" — flat namespace matches the
    :class:`SecretStore` contract.

    Probe-on-construct: if the keyring backend can't be reached
    (no D-Bus session, no daemon running, ``keyring`` package not
    installed in this Python), :class:`SecretServiceUnavailable`
    fires from ``__init__``. Callers branch on it to fall back to
    JSON (with explicit opt-in per H.5) or to surface an error.

    Once constructed, ``get`` returns ``None`` for missing keys
    (matching :class:`JsonFallbackStore`); ``delete`` is a no-op
    for missing keys (also matching). All other backend errors at
    runtime re-raise as :class:`SecretServiceUnavailable` so
    callers see a single typed exception class regardless of
    backend.
    """

    def __init__(self, keyring_module: Any | None = None) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module  # type: ignore[no-redef]
                import keyring.errors  # noqa: F401  ensure submodule loadable
            except ImportError as exc:
                raise SecretServiceUnavailable(
                    "keyring package not available in this Python"
                ) from exc
        self._keyring = keyring_module
        # Cache the typed delete-missing exception; defensive about
        # fakes that don't expose .errors (tests use a complete fake;
        # real keyring always exposes it).
        errors_mod = getattr(keyring_module, "errors", None)
        self._password_delete_error = getattr(
            errors_mod, "PasswordDeleteError", Exception,
        )
        # Probe: get_password is non-side-effecting and forces
        # backend init. None on missing entry is fine; failure
        # raises and we surface as SecretServiceUnavailable.
        try:
            self._keyring.get_password(SERVICE_NAME, "_probe")
        except SecretServiceUnavailable:
            raise
        except Exception as exc:
            raise SecretServiceUnavailable(
                f"keyring probe failed: {exc}"
            ) from exc

    def is_secure(self) -> bool:
        return True

    def get(self, key: str) -> str | None:
        try:
            return self._keyring.get_password(SERVICE_NAME, key)
        except Exception as exc:
            raise SecretServiceUnavailable(
                f"keyring get failed for {key!r}: {exc}"
            ) from exc

    def set(self, key: str, value: str) -> None:
        try:
            self._keyring.set_password(SERVICE_NAME, key, value)
        except Exception as exc:
            raise SecretServiceUnavailable(
                f"keyring set failed for {key!r}: {exc}"
            ) from exc

    def delete(self, key: str) -> None:
        try:
            self._keyring.delete_password(SERVICE_NAME, key)
        except self._password_delete_error:
            # No such entry — match JsonFallbackStore.delete semantics.
            return
        except Exception as exc:
            raise SecretServiceUnavailable(
                f"keyring delete failed for {key!r}: {exc}"
            ) from exc
