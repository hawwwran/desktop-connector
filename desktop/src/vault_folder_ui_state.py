"""Vault folders tab state helpers.

The GTK window stays thin: it renders rows from these pure helpers and
delegates encrypted manifest writes to ``vault.Vault``.
"""

from __future__ import annotations

from typing import Any


DEFAULT_FOLDER_IGNORE_PATTERNS = [".git/", "node_modules/", "*.tmp"]
FOLDER_COLUMNS = ["Name", "Binding", "Current", "Stored", "History", "Status"]


def default_ignore_patterns_text() -> str:
    """Editable text shown in the Add-folder dialog."""
    return "\n".join(DEFAULT_FOLDER_IGNORE_PATTERNS) + "\n"


def parse_ignore_patterns_text(text: str) -> list[str]:
    """Parse the editable ignore-pattern text area.

    Blank lines are ignored and duplicates are removed while preserving
    first occurrence order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in str(text).splitlines():
        pattern = raw.strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        out.append(pattern)
    return out


def folder_rows_from_cache(
    folders: list[dict[str, Any]],
    *,
    bindings: dict[str, str] | None = None,
    usage_by_folder: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, str]]:
    """Return render-ready Folders-tab rows from cached manifest metadata."""
    bindings = bindings or {}
    usage_by_folder = usage_by_folder or {}
    rows: list[dict[str, str]] = []
    for folder in folders:
        remote_folder_id = str(folder.get("remote_folder_id", ""))
        usage = usage_by_folder.get(remote_folder_id, {})
        state = str(folder.get("state", "active"))
        rows.append({
            "remote_folder_id": remote_folder_id,
            "name": str(folder.get("display_name_enc") or remote_folder_id or "(unnamed)"),
            "binding": bindings.get(remote_folder_id, "Not bound"),
            "current": _format_bytes(int(usage.get("current_bytes", 0))),
            "stored": _format_bytes(int(usage.get("stored_bytes", 0))),
            "history": _format_bytes(int(usage.get("history_bytes", 0))),
            "status": state[:1].upper() + state[1:] if state else "",
        })
    return rows


def _format_bytes(value: int) -> str:
    value = max(0, int(value))
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value // 1024} KB"
    return f"{value / (1024 * 1024):.1f} MB"


BINDING_COLUMNS = ["Local path", "Remote folder", "State", "Sync mode", "Synced rev"]


def binding_rows_for_render(
    bindings: list[Any],
    *,
    folder_names_by_id: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Render-ready rows for the Bindings panel (T10.6).

    ``bindings`` is a list of :class:`vault_bindings.VaultBinding` rows
    (or any object with the same attribute surface). ``folder_names_by_id``
    maps remote_folder_id → display name so rows can show the folder's
    human label instead of an opaque id.
    """
    folder_names_by_id = folder_names_by_id or {}
    rows: list[dict[str, str]] = []
    for binding in bindings:
        remote_folder_id = str(getattr(binding, "remote_folder_id", ""))
        rows.append({
            "binding_id": str(getattr(binding, "binding_id", "")),
            "local_path": str(getattr(binding, "local_path", "")),
            "remote_folder": folder_names_by_id.get(
                remote_folder_id, remote_folder_id,
            ),
            "remote_folder_id": remote_folder_id,
            "state": str(getattr(binding, "state", "")),
            "sync_mode": str(getattr(binding, "sync_mode", "")),
            "last_synced_revision": str(
                int(getattr(binding, "last_synced_revision", 0) or 0)
            ),
        })
    return rows
