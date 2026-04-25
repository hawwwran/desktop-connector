"""Migrate from a classic apt-pip install on first AppImage launch (P.4b).

Detects the layout that ``desktop/install.sh`` lays down
(`~/.local/share/desktop-connector/src/main.py` + `~/.local/bin/desktop-connector`
shell wrapper) and, after verifying the existing config+keys still
parse cleanly and round-trip to the recorded device_id, removes those
files so the AppImage is the sole installation on the machine.

The user's config dir (`~/.config/desktop-connector/`) is **not**
touched — both install paths share it byte-for-byte, so pairings,
history, and keys survive the migration unchanged. The install hook
(P.3b) rewrites the autostart entry to point at $APPIMAGE on the same
launch, completing the cutover.

Verification model — copy-then-verify-then-delete (no actual copy
needed since both paths share the config dir):

  1. Confirm config.json is parseable.
  2. Confirm the keypair loads.
  3. If the user is registered (config.device_id present): confirm
     SHA-256(public_key)[:32] == config.device_id. A mismatch means
     someone manually edited the keys; bail out without touching the
     old install so the user can investigate.

On verification failure: surface a warning notification and leave
both installs in place. On success: rm -rf the old install dir,
remove the bin wrapper, fire one info notification.

No-op when $APPIMAGE is unset (dev tree, classic install).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

from ..config import Config
from ..crypto import KeyManager
from ..interfaces.notifications import NotificationBackend

log = logging.getLogger("desktop-connector")

OLD_INSTALL_DIR = Path.home() / ".local/share/desktop-connector"
OLD_LAUNCHER = Path.home() / ".local/bin/desktop-connector"
# install.sh lays src/main.py inside OLD_INSTALL_DIR. Its presence is the
# marker; absence (orphan keys, etc.) means there's nothing to migrate.
OLD_INSTALL_MARKER = OLD_INSTALL_DIR / "src" / "main.py"


def migrate_from_apt_pip_if_needed(
    config: Config,
    crypto: KeyManager,
    notifications: NotificationBackend,
) -> None:
    """Verify + clean up an apt-pip install when running from an AppImage.

    Safe to call on every launch — exits early if the marker isn't
    present or we're not inside an AppImage.
    """
    if not os.environ.get("APPIMAGE"):
        return
    if not OLD_INSTALL_MARKER.exists():
        return

    log.info("appimage.migration.detected old_install_dir=%s", OLD_INSTALL_DIR)

    if not _verify_state(config, crypto):
        log.warning("appimage.migration.verification_failed leaving_old_install_in_place")
        try:
            notifications.notify(
                "Could not migrate classic install",
                "Your existing install at ~/.local/share/desktop-connector/ "
                "looks inconsistent with your config. The AppImage will run "
                "alongside it for now — review and remove the old install manually.",
            )
        except Exception:
            log.exception("appimage.migration.notify_failed")
        return

    _remove_old_install()
    log.info("appimage.migration.cleanup_complete")
    try:
        notifications.notify(
            "Migrated from classic install",
            "Your pairings and history are preserved. The AppImage now "
            "manages this app — you can safely remove the old installer file.",
        )
    except Exception:
        log.exception("appimage.migration.notify_failed")


def _verify_state(config: Config, crypto: KeyManager) -> bool:
    """Sanity-check that the user's config + keys still round-trip.

    Returns False on any mismatch; the caller leaves the old install in
    place so the user can investigate.
    """
    try:
        # Config and KeyManager were already constructed by the parent;
        # if those raised, we wouldn't be here. The remaining check is the
        # public-key → device_id fingerprint match, which catches the case
        # where a user manually edited keys/private_key.pem.
        if config.device_id:
            derived = _derive_device_id(crypto)
            if derived != config.device_id:
                log.warning(
                    "appimage.migration.fingerprint_mismatch derived=%s recorded=%s",
                    derived[:12],
                    config.device_id[:12],
                )
                return False
        return True
    except Exception:
        log.exception("appimage.migration.verification_raised")
        return False


def _derive_device_id(crypto: KeyManager) -> str:
    raw = crypto.get_public_key_bytes()
    return hashlib.sha256(raw).hexdigest()[:32]


def _remove_old_install() -> None:
    """Wipe the apt-pip install dir + launcher wrapper.

    The whole `~/.local/share/desktop-connector/` tree is owned by
    install.sh's layout (every file in there came from the installer,
    no user data lives at this path), so a recursive delete is safe.
    """
    try:
        shutil.rmtree(OLD_INSTALL_DIR)
        log.info("appimage.migration.removed_install_dir path=%s", OLD_INSTALL_DIR)
    except OSError as e:
        log.warning("appimage.migration.remove_install_dir_failed error=%s", e)

    try:
        if OLD_LAUNCHER.exists() or OLD_LAUNCHER.is_symlink():
            OLD_LAUNCHER.unlink()
            log.info("appimage.migration.removed_launcher path=%s", OLD_LAUNCHER)
    except OSError as e:
        log.warning("appimage.migration.remove_launcher_failed error=%s", e)
