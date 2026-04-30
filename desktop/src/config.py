"""
Configuration management for Desktop Connector.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .secrets import (
    SECRET_KEY_AUTH_TOKEN,
    JsonFallbackStore,
    SecretServiceUnavailable,
    SecretStore,
    open_default_store,
    pairing_symkey_key,
)

# Test-only escape hatch: when set, Config defaults to the JSON
# fallback store instead of trying the OS keyring. tests/protocol/
# sets this so the test runner never writes to the dev machine's
# real keyring. Production code never sets this.
_NO_KEYRING_ENV_VAR = "DESKTOP_CONNECTOR_NO_KEYRING"


@dataclass(frozen=True)
class ScrubResult:
    """Outcome of :meth:`Config.scrub_secrets`.

    ``secure`` is the active backend's status (False = JSON
    fallback; scrub couldn't act). ``scrubbed`` is the count of
    plaintext fields removed from ``config.json``; ``failed`` is
    the count that couldn't be migrated (remained as plaintext
    for the next boot to retry).
    """
    secure: bool
    scrubbed: int
    failed: int

log = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "desktop-connector"
DEFAULT_SAVE_DIR = Path.home() / "Desktop-Connector"
DEFAULT_SERVER_URL = "http://localhost:4441"
DEFAULT_POLL_INTERVAL = 30  # seconds when idle
FAST_POLL_INTERVAL = 5      # seconds after a transfer is found
FAST_POLL_DURATION = 120    # seconds to stay in fast-poll mode

# Restrictive perms on the config directory + file: secrets like
# auth_token and per-pairing symmetric keys live in config.json
# until hardening H.2+ moves them to the OS secret store.
CONFIG_DIR_MODE = 0o700
CONFIG_FILE_MODE = 0o600

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

    def __init__(
        self,
        config_dir: Path | None = None,
        secret_store: SecretStore | None = None,
    ):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=CONFIG_DIR_MODE)
        try:
            os.chmod(self.config_dir, CONFIG_DIR_MODE)
        except OSError as exc:
            log.warning(
                "config.permissions.dir_chmod_failed dir=%s err=%s",
                self.config_dir, exc,
            )
        self.config_file = self.config_dir / "config.json"
        self._warn_if_weak_perms()
        self._data = self._load()
        # H.4: select the secret-store backend. Production tries the
        # OS Secret Service (libsecret/KWallet) and falls back to the
        # JSON fallback if unreachable; tests set
        # DESKTOP_CONNECTOR_NO_KEYRING=1 to keep their writes off the
        # dev machine's real keyring. Callers can override entirely
        # by passing secret_store=.
        if secret_store is not None:
            self._secret_store: SecretStore = secret_store
        elif os.environ.get(_NO_KEYRING_ENV_VAR):
            self._secret_store = JsonFallbackStore(self._data, self.save)
        else:
            self._secret_store = open_default_store(self._data, self.save)
        self._migrate_legacy_secrets_if_needed()
        self._migrate_receive_actions()
        self._migrate_receive_action_limits()

    def _warn_if_weak_perms(self) -> None:
        """Log once at startup if config.json has group/world bits.

        We don't chmod existing files in place — a concurrent reader
        might be in the middle of an old open(). The next save() runs
        through the atomic-rename path which always lands a 0o600 file,
        so this self-heals on first write.
        """
        if not self.config_file.exists():
            return
        try:
            mode = self.config_file.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            log.warning(
                "config.permissions.weak path=%s mode=%o expected=%o "
                "(fixed on next save)",
                self.config_file, mode, CONFIG_FILE_MODE,
            )

    def _load(self) -> dict:
        if self.config_file.exists():
            with open(self.config_file) as f:
                return json.load(f)
        return {}

    def save(self) -> None:
        """Atomic save with restrictive permissions.

        Writes to a per-PID tmp file in the same directory, sets mode
        0o600 BEFORE rename so the visible file at the target path is
        never world/group-readable, then atomically renames over the
        target. Side-effect: pre-H.1 installs (with 0o644 files)
        self-heal on their first save.
        """
        tmp = self.config_dir / f".config.json.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.chmod(tmp, CONFIG_FILE_MODE)
            os.replace(tmp, self.config_file)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _migrate_legacy_secrets_if_needed(self) -> None:
        """Move plaintext secrets from config.json into the secret store.

        Triggered once on every startup. No-op when the active store
        is the JSON fallback (insecure backend can't help) or when
        the JSON has no plaintext secrets to migrate. On partial
        failure (a single ``store.set`` raises), leaves remaining
        plaintext in place — the next boot retries.

        Per H.4 acceptance: a ``cat config.json`` after a successful
        migration shows neither ``auth_token`` nor any pairing
        ``symmetric_key_b64``. A ``secrets_migrated_at`` ISO-8601
        marker records when the move happened.
        """
        if not self._secret_store.is_secure():
            return

        migrated_keys: list[str] = []

        legacy_token = self._data.get("auth_token")
        if isinstance(legacy_token, str) and legacy_token:
            try:
                self._secret_store.set(SECRET_KEY_AUTH_TOKEN, legacy_token)
                del self._data["auth_token"]
                migrated_keys.append("auth_token")
            except SecretServiceUnavailable as exc:
                log.warning(
                    "config.secrets.migration_failed key=auth_token reason=%s "
                    "(plaintext left in place; retrying next boot)",
                    exc,
                )

        for device_id, entry in list(self._data.get("paired_devices", {}).items()):
            if not isinstance(entry, dict):
                continue
            legacy_skey = entry.get("symmetric_key_b64")
            if not isinstance(legacy_skey, str) or not legacy_skey:
                continue
            try:
                self._secret_store.set(
                    pairing_symkey_key(device_id), legacy_skey,
                )
                del entry["symmetric_key_b64"]
                migrated_keys.append(f"pairing_symkey:{device_id[:12]}")
            except SecretServiceUnavailable as exc:
                log.warning(
                    "config.secrets.migration_failed "
                    "key=pairing_symkey:%s reason=%s "
                    "(plaintext left in place; retrying next boot)",
                    device_id[:12], exc,
                )

        if migrated_keys:
            # ISO-8601 UTC marker; useful for debugging "did this
            # install ever migrate?" without touching the keyring.
            from datetime import datetime, timezone
            self._data["secrets_migrated_at"] = (
                datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            self.save()
            log.info(
                "config.secrets.migrated count=%d keys=%s",
                len(migrated_keys), ",".join(migrated_keys),
            )

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
        return self._secret_store.get(SECRET_KEY_AUTH_TOKEN)

    @auth_token.setter
    def auth_token(self, value: str) -> None:
        # store.set commits durably (saves the JSON dict for the
        # JsonFallbackStore; writes to libsecret for SecretServiceStore).
        # Callers don't need a separate self.save().
        self._secret_store.set(SECRET_KEY_AUTH_TOKEN, value)

    @property
    def device_id(self) -> str | None:
        return self._data.get("device_id")

    @device_id.setter
    def device_id(self, value: str) -> None:
        self._data["device_id"] = value
        self.save()

    @property
    def active_device_id(self) -> str | None:
        value = self._data.get("active_device_id")
        return value if isinstance(value, str) and value else None

    @active_device_id.setter
    def active_device_id(self, value: str | None) -> None:
        self.reload()
        if value:
            self._data["active_device_id"] = value
        else:
            self._data.pop("active_device_id", None)
        self.save()

    def scrub_secrets(self) -> "ScrubResult":
        """Reload from disk, migrate any plaintext into the secret
        store, return what happened.

        Useful when:
          - User manually edited ``config.json`` to add ``auth_token``
            or ``symmetric_key_b64`` back in.
          - A prior boot's automatic migration partial-failed (e.g.
            keyring was locked at startup), leaving plaintext for
            the failed key in ``config.json``.
          - Caller just wants to confirm "no plaintext anywhere".

        On an insecure store (JSON fallback active), scrub is a no-op
        — there's no secure backend to migrate INTO. Returns
        ``ScrubResult(secure=False, scrubbed=0, failed=0)``.

        On a secure store with no plaintext to migrate, returns
        ``ScrubResult(secure=True, scrubbed=0, failed=0)``.

        On partial failure, ``failed`` is non-zero and the failed
        plaintext stays in ``config.json`` for next boot to retry.
        """
        if not self._secret_store.is_secure():
            log.info("config.secrets.scrub.skipped reason=insecure_store")
            return ScrubResult(secure=False, scrubbed=0, failed=0)

        self.reload()
        before = self._count_plaintext_secrets()
        self._migrate_legacy_secrets_if_needed()
        after = self._count_plaintext_secrets()
        scrubbed = max(before - after, 0)
        result = ScrubResult(secure=True, scrubbed=scrubbed, failed=after)
        log.info(
            "config.secrets.scrub.result secure=True scrubbed=%d failed=%d",
            result.scrubbed, result.failed,
        )
        return result

    def _count_plaintext_secrets(self) -> int:
        """Count of plaintext secret fields currently in self._data.

        Used by :meth:`scrub_secrets` to compute before/after deltas.
        Counts ``auth_token`` (top-level) plus each non-empty
        ``paired_devices[*].symmetric_key_b64``.
        """
        n = 0
        token = self._data.get("auth_token")
        if isinstance(token, str) and token:
            n += 1
        for entry in self._data.get("paired_devices", {}).values():
            if isinstance(entry, dict):
                value = entry.get("symmetric_key_b64")
                if isinstance(value, str) and value:
                    n += 1
        return n

    @property
    def secret_store(self) -> SecretStore:
        """Read-only access to the active secret store.

        Production callers (notably :class:`crypto.KeyManager` per
        H.7) need the same store backend Config selected so the
        private key lands alongside auth_token + pairing symkeys.
        Returning the underlying instance keeps the seam in one place
        — H.4 set the policy via ``open_default_store``; H.7 reuses
        that selection rather than independently re-probing the
        keyring.
        """
        return self._secret_store

    def is_secret_storage_secure(self) -> bool:
        """True iff the active secret-store backend is the OS keyring
        (libsecret / KWallet via :class:`SecretServiceStore`).

        False means JSON fallback is in effect — secrets sit in
        plaintext ``config.json`` (with H.1's ``chmod 0600``). H.5
        surfaces this state via a CLI warning at startup and a
        clickable tray menu indicator.
        """
        return self._secret_store.is_secure()

    def reload(self) -> None:
        """Reload config from disk (picks up changes from subprocesses).

        Mutates ``self._data`` in place rather than reassigning the
        attribute — the secret store holds a reference to this dict
        (H.2's JsonFallbackStore), so reassignment would silently
        leave the store pointing at the old snapshot.
        """
        new_data = self._load()
        self._data.clear()
        self._data.update(new_data)
        self._migrate_receive_actions()
        self._migrate_receive_action_limits()

    @property
    def paired_devices(self) -> dict:
        """Returns dict of {device_id: {pubkey, symmetric_key_b64, name, paired_at}}.

        Hydrates ``symmetric_key_b64`` from the secret store on each
        access so callers see the same dict shape regardless of which
        backend is active. Post-H.4 the JSON dict no longer carries
        the symkey when SecretServiceStore is in use; this getter
        merges it back from the keyring.

        The returned dict is freshly constructed — mutations don't
        persist. Use :meth:`add_paired_device` / :meth:`remove_paired_device`
        for changes; the legacy direct-dict-mutation pattern (e.g.
        ``del cfg._data["paired_devices"][id]; cfg.save()``) would
        leak keyring entries and is no longer safe.
        """
        self.reload()
        raw = self._data.get("paired_devices", {})
        result: dict = {}
        for device_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            merged = dict(entry)
            if "symmetric_key_b64" not in merged:
                try:
                    symkey = self._secret_store.get(pairing_symkey_key(device_id))
                except SecretServiceUnavailable:
                    symkey = None
                if symkey is not None:
                    merged["symmetric_key_b64"] = symkey
            result[device_id] = merged
        return result

    def get_pairing_symkey(self, device_id: str) -> str | None:
        """Fast lookup of a single pairing's symmetric key.

        Reloads before checking membership so long-running tray/find
        processes see pairings added by a pairing subprocess before
        deciding whether an inbound fasttrack sender is unknown.
        Avoids the full ``paired_devices`` rebuild and reads either
        the hydrated symkey (JsonFallbackStore) or the secret store
        directly (SecretServiceStore). Returns ``None`` for unknown
        devices or when the secret store is unreachable.
        """
        self.reload()
        entry = self._data.get("paired_devices", {}).get(device_id)
        if not isinstance(entry, dict):
            return None
        symkey = entry.get("symmetric_key_b64")
        if isinstance(symkey, str) and symkey:
            return symkey
        try:
            return self._secret_store.get(pairing_symkey_key(device_id))
        except SecretServiceUnavailable:
            return None

    def add_paired_device(self, device_id: str, pubkey: str, symmetric_key_b64: str, name: str = "") -> None:
        # Non-secret pairing metadata stays in _data. The symmetric
        # key — the only secret — goes through the secret store.
        # JsonFallbackStore writes it back into the same paired_devices
        # entry so the on-disk shape is byte-equivalent to pre-H.2.
        # H.3+'s SecretServiceStore will land it in libsecret instead;
        # callers of this method don't need to know which one is in
        # use.
        if "paired_devices" not in self._data:
            self._data["paired_devices"] = {}
        self._data["paired_devices"][device_id] = {
            "pubkey": pubkey,
            "name": name,
            "paired_at": int(time.time()),
        }
        # store.set commits the JSON (or libsecret) and saves _data,
        # which now also carries the metadata above. SecretServiceStore
        # writes only the secret, so save metadata explicitly in that
        # secure-store path.
        self._secret_store.set(pairing_symkey_key(device_id), symmetric_key_b64)
        if self._secret_store.is_secure():
            self.save()

    def rename_paired_device(self, device_id: str, name: str) -> bool:
        """Update only non-secret pairing metadata.

        Returns False when the pairing no longer exists. This method
        intentionally edits ``_data`` directly instead of assigning a
        hydrated ``paired_devices`` snapshot back into config, which
        would reintroduce secure-store symmetric keys into config.json.
        """
        self.reload()
        paired = self._data.get("paired_devices", {})
        entry = paired.get(device_id)
        if not isinstance(entry, dict):
            return False
        entry["name"] = name
        self.save()
        return True

    def remove_paired_device(self, device_id: str) -> None:
        """Remove a single paired device, including its keyring entry.

        Use this instead of mutating ``cfg._data["paired_devices"]``
        directly — without going through the secret store, removing
        an entry from the JSON dict would leak its libsecret /
        keyring entry. Also: the :attr:`paired_devices` getter now
        hydrates symkeys from the store, so re-assigning a hydrated
        dict back to ``_data`` would re-introduce plaintext into
        ``config.json``. ``remove_paired_device`` avoids both
        traps.
        """
        self.reload()
        try:
            self._secret_store.delete(pairing_symkey_key(device_id))
        except SecretServiceUnavailable as exc:
            # Keyring transient failure: leave the entry hanging
            # rather than corrupting state mid-removal. Caller can
            # retry; orphans are harmless until next wipe_credentials.
            log.warning(
                "config.secrets.delete_failed key=pairing_symkey:%s reason=%s",
                device_id[:12], exc,
            )
            return
        paired = self._data.get("paired_devices", {})
        if device_id in paired:
            del paired[device_id]
            if self._data.get("active_device_id") == device_id:
                self._data.pop("active_device_id", None)
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
        self.reload()
        # Walk paired_devices first and ask the store to delete each
        # symkey — H.3+'s libsecret backend uses this to clear orphan
        # keyring entries that a bare _data.pop() would leave behind.
        # JsonFallbackStore is happy to delete fields it'll then drop
        # via the pop below.
        for device_id in list(self._data.get("paired_devices", {}).keys()):
            self._secret_store.delete(pairing_symkey_key(device_id))
        self._data.pop("paired_devices", None)
        self._data.pop("active_device_id", None)
        if scope == "full":
            self._secret_store.delete(SECRET_KEY_AUTH_TOKEN)
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
        self.reload()
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
