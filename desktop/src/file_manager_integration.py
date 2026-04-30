"""File-manager integration sync for paired connected devices.

Generates one Nautilus/Nemo script per paired device named
``Send to <device-name>`` and a single Dolphin service-menu file with
one action per device. Every managed entry carries a sentinel plus the
pairing's device id so the sync can identify our own files and never
touch user-created scripts that happen to share a name.

Idempotent: safe to call at startup, after a pairing save, after a
Settings rename, and after a Settings unpair. Dev-tree runs that have
neither ``$APPIMAGE`` nor ``~/.local/bin/desktop-connector`` are a
no-op — there's no installed launcher to wire scripts into.

Legacy single-pair scripts created by older AppImage hooks or by
``install-from-source.sh`` are recognized via fingerprint and removed
on first sync so the multi-device targets take over cleanly.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from .config import Config
from .devices import ConnectedDevice, ConnectedDeviceRegistry

log = logging.getLogger("desktop-connector")

APP_NAME = "desktop-connector"
DOLPHIN_SERVICE_FILENAME = f"{APP_NAME}-send.desktop"
LEGACY_NAUTILUS_NAME = "Send to Phone"

MANAGED_SENTINEL = "desktop-connector:managed-fm-target"
PAIRING_ID_PREFIX = "desktop-connector:pairing-id="

# Telltale substrings of legacy single-pair file-manager scripts
# (AppImage hook + contributor nautilus-send-to-phone.py templates).
_LEGACY_SCRIPT_FINGERPRINTS = (
    "Send selected files to phone via Desktop Connector",
)
_LEGACY_DOLPHIN_FINGERPRINTS = (
    "Name=Send to Phone",
)


def sync_file_manager_targets(
    config: Config,
    *,
    appimage_path: Path | None = None,
    source_bin_path: Path | None = None,
    home: Path | None = None,
    file_managers: set[str] | None = None,
) -> None:
    """Re-sync per-device file-manager send targets.

    Resolves the launcher in this order:

    * explicit ``appimage_path`` argument,
    * ``$APPIMAGE`` environment variable,
    * explicit ``source_bin_path`` argument,
    * ``~/.local/bin/desktop-connector`` if it exists.

    No-op when none of those resolve. Tests can pass ``home``,
    ``appimage_path``, ``source_bin_path``, and ``file_managers``
    directly to avoid touching the developer's real environment.
    """
    if home is None:
        home = Path.home()

    if appimage_path is None:
        env_appimage = os.environ.get("APPIMAGE")
        if env_appimage:
            appimage_path = Path(env_appimage)

    if source_bin_path is None:
        candidate = home / ".local/bin" / APP_NAME
        if candidate.exists():
            source_bin_path = candidate

    launcher = _invocation_command(appimage_path, source_bin_path)
    if launcher is None:
        log.debug("file_manager.sync.skipped reason=no_launcher")
        return

    if file_managers is None:
        file_managers = _detect_file_managers()

    registry = ConnectedDeviceRegistry(config)
    devices = registry.list_devices()  # also normalizes duplicates

    nautilus_dir = home / ".local/share/nautilus/scripts"
    nemo_dir = home / ".local/share/nemo/scripts"
    dolphin_path = (
        home / ".local/share/kservices5/ServiceMenus" / DOLPHIN_SERVICE_FILENAME
    )

    if "nautilus" in file_managers:
        _sync_script_dir(nautilus_dir, devices, launcher, "nautilus")
    if "nemo" in file_managers:
        _sync_script_dir(nemo_dir, devices, launcher, "nemo")
    if "dolphin" in file_managers:
        _sync_dolphin_service(dolphin_path, devices, launcher)


def _detect_file_managers() -> set[str]:
    found: set[str] = set()
    for name in ("nautilus", "nemo", "dolphin"):
        if shutil.which(name):
            found.add(name)
    return found


def _invocation_command(
    appimage_path: Path | None,
    source_bin_path: Path | None,
) -> str | None:
    if appimage_path is not None:
        return str(appimage_path)
    if source_bin_path is not None:
        return str(source_bin_path)
    return None


# Strips control chars, path separators, and FS-hostile punctuation
# while preserving Unicode letters/digits — paired-device names can be
# Czech / German / Japanese / etc. and we want the script filename to
# reflect that. `pairing_key.default_filename` uses an ASCII-only
# allowlist for its `.dcpair` export dialog where ASCII is the norm;
# this site is per-pair file-manager integration where Unicode names
# need to survive.
_FILENAME_BLOCKED = re.compile(r"[\x00-\x1f/\\:*?\"<>|]+")


def _safe_filename_component(name: str) -> str:
    cleaned = _FILENAME_BLOCKED.sub("-", name).strip().strip(".")
    return cleaned or "Device"


def _script_filename(device: ConnectedDevice) -> str:
    return f"Send to {_safe_filename_component(device.name)}"


def _action_id(device: ConnectedDevice) -> str:
    return f"sendToDevice_{device.short_id}"


# --- Nautilus / Nemo scripts ----------------------------------------

_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""Send selected files to "{display_name}" via Desktop Connector.

Auto-managed by Desktop Connector. Edits are overwritten on the next
sync. Delete to remove the entry; it stays gone until the device is
re-paired or renamed.
"""
# {sentinel}
# {pairing_id_prefix}{pairing_id}
import os
import subprocess
import sys

LAUNCHER = "{launcher}"
TARGET_DEVICE_ID = "{pairing_id}"
DEVICE_NAME = "{display_name}"


def main():
    paths_str = os.environ.get("NAUTILUS_SCRIPT_SELECTED_FILE_PATHS", "")
    if not paths_str.strip():
        paths_str = os.environ.get("NEMO_SCRIPT_SELECTED_FILE_PATHS", "")
    if not paths_str.strip():
        paths = sys.argv[1:]
    else:
        paths = [p for p in paths_str.strip().split("\\n") if p]

    if not paths:
        subprocess.run(["notify-send", "-a", "Desktop Connector", "No files selected"])
        return

    files = [p for p in paths if os.path.isfile(p)]
    folders = [p for p in paths if os.path.isdir(p)]

    for path in files:
        subprocess.Popen([
            LAUNCHER, "--headless", "--send=" + path,
            "--target-device-id=" + TARGET_DEVICE_ID,
        ])

    if folders:
        word = "folder" if len(folders) == 1 else "folders"
        subprocess.run([
            "notify-send", "-a", "Desktop Connector", "-i", "dialog-warning",
            "Folder transport is not supported",
            f"Skipped {{len(folders)}} {{word}}. Send individual files instead.",
        ])

    if files:
        subprocess.run([
            "notify-send", "-a", "Desktop Connector",
            f"Sending to {{DEVICE_NAME}}",
            f"{{len(files)}} file(s) queued",
        ])


if __name__ == "__main__":
    main()
'''


def _script_text(device: ConnectedDevice, launcher: str) -> str:
    safe_name = device.name.replace('"', "'").replace("\\", "\\\\")
    return _SCRIPT_TEMPLATE.format(
        display_name=safe_name,
        launcher=launcher.replace('"', "'").replace("\\", "\\\\"),
        sentinel=MANAGED_SENTINEL,
        pairing_id_prefix=PAIRING_ID_PREFIX,
        pairing_id=device.device_id,
    )


def _is_managed_script(content: str) -> bool:
    return MANAGED_SENTINEL in content


def _is_legacy_script(content: str) -> bool:
    return any(fp in content for fp in _LEGACY_SCRIPT_FINGERPRINTS)


def _extract_pairing_id(content: str) -> str | None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# " + PAIRING_ID_PREFIX):
            return stripped[len("# " + PAIRING_ID_PREFIX) :].strip()
    return None


def _sync_script_dir(
    scripts_dir: Path,
    devices: list[ConnectedDevice],
    launcher: str,
    kind: str,
) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)

    desired_filename_by_id = {
        device.device_id: _script_filename(device) for device in devices
    }

    # Pass 1: clean managed entries that are stale, plus legacy
    # single-pair scripts that the new sync supersedes.
    for entry in list(scripts_dir.iterdir()):
        if not entry.is_file():
            continue
        try:
            content = entry.read_text()
        except OSError:
            continue

        if entry.name == LEGACY_NAUTILUS_NAME and _is_legacy_script(content):
            try:
                entry.unlink()
                log.info("file_manager.%s.legacy_removed name=%s",
                         kind, entry.name)
            except OSError as exc:
                log.warning("file_manager.%s.legacy_remove_failed: %s",
                            kind, exc)
            continue

        if not _is_managed_script(content):
            continue  # never touch unmanaged user files

        pairing_id = _extract_pairing_id(content)
        expected = desired_filename_by_id.get(pairing_id) if pairing_id else None
        if expected is None or expected != entry.name:
            try:
                entry.unlink()
                log.info("file_manager.%s.cleaned name=%s peer=%s",
                         kind, entry.name,
                         (pairing_id or "")[:12])
            except OSError as exc:
                log.warning("file_manager.%s.cleanup_failed: %s", kind, exc)

    # Pass 2: write/refresh the per-device script for each pair.
    for device in devices:
        target = scripts_dir / _script_filename(device)
        new_content = _script_text(device, launcher)
        if target.exists():
            try:
                existing = target.read_text()
                if existing == new_content:
                    target.chmod(0o755)
                    continue
                if not (_is_managed_script(existing) or _is_legacy_script(existing)):
                    log.warning(
                        "file_manager.%s.skip_unmanaged_collision name=%s",
                        kind, target.name,
                    )
                    continue
            except OSError as exc:
                log.warning(
                    "file_manager.%s.skip_unreadable_collision name=%s error=%s",
                    kind, target.name, exc,
                )
                continue
        _atomic_write(target, new_content)
        target.chmod(0o755)
        log.info(
            "file_manager.%s.write peer=%s name=%s",
            kind, device.short_id, target.name,
        )


# --- Dolphin service menu -------------------------------------------

def _dolphin_service_text(
    devices: list[ConnectedDevice],
    launcher: str,
) -> str:
    actions = [_action_id(d) for d in devices]
    pairing_marker = " ".join(d.device_id for d in devices)
    lines = [
        f"# {MANAGED_SENTINEL}",
        f"# {PAIRING_ID_PREFIX}{pairing_marker}",
        "[Desktop Entry]",
        "Type=Service",
        "ServiceTypes=KonqPopupMenu/Plugin",
        "MimeType=application/octet-stream;",
        "Actions=" + ";".join(actions),
        "",
    ]
    for device in devices:
        lines.append(f"[Desktop Action {_action_id(device)}]")
        lines.append(f"Name=Send to {device.name}")
        lines.append(f"Icon={APP_NAME}")
        lines.append(
            f"Exec={launcher} --headless --send=%f "
            f"--target-device-id={device.device_id}"
        )
        lines.append("")
    return "\n".join(lines)


def _is_managed_dolphin(content: str) -> bool:
    return MANAGED_SENTINEL in content


def _is_legacy_dolphin(content: str) -> bool:
    return any(fp in content for fp in _LEGACY_DOLPHIN_FINGERPRINTS)


def _sync_dolphin_service(
    path: Path,
    devices: list[ConnectedDevice],
    launcher: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not devices:
        if path.exists():
            try:
                content = path.read_text()
            except OSError:
                content = ""
            if _is_managed_dolphin(content) or _is_legacy_dolphin(content):
                try:
                    path.unlink()
                    log.info("file_manager.dolphin.removed reason=no_pairs")
                except OSError as exc:
                    log.warning(
                        "file_manager.dolphin.remove_failed: %s", exc,
                    )
        return

    new_content = _dolphin_service_text(devices, launcher)
    if path.exists():
        try:
            existing = path.read_text()
        except OSError as exc:
            log.warning("file_manager.dolphin.skip_unreadable_collision: %s", exc)
            return
        if existing == new_content:
            return
        if not (_is_managed_dolphin(existing) or _is_legacy_dolphin(existing)):
            log.warning("file_manager.dolphin.skip_unmanaged_collision")
            return

    _atomic_write(path, new_content)
    log.info("file_manager.dolphin.written peers=%d", len(devices))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)
