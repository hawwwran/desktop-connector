"""Data lookups + async usage refresh for the Folders tab.

These helpers used to be nested closures inside
``build_vault_folders_tab``. They are now module-level functions taking
the shared :class:`FoldersContext`. Behaviour, threading, and
``GLib.idle_add`` boundaries are byte-identical to the pre-split
original.
"""

from __future__ import annotations

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from ..vault_bindings import VaultBindingsStore
from ..vault_error_messages import humanize
from ..vault_folder_ui_state import (
    binding_rows_for_render,
    folder_rows_from_cache,
)
from ..vault_usage import calculate_vault_usage
from .context import FoldersContext


def list_folders(ctx: FoldersContext) -> list[dict]:
    """Return render-ready folder rows for the sidebar."""
    if not ctx.vault_id:
        return []
    try:
        cached = ctx.local_index.list_remote_folders(ctx.vault_id)
    except Exception as exc:  # noqa: BLE001
        ctx.set_sidebar_status(f"Could not load folder cache: {exc}", "error")
        return []
    return folder_rows_from_cache(
        cached, usage_by_folder=ctx.usage_by_folder_state["value"] or {},
    )


def list_bindings_for_folder(
    ctx: FoldersContext, remote_folder_id: str,
) -> list[dict]:
    """Return render-ready binding rows scoped to one remote folder."""
    if not ctx.vault_id or not remote_folder_id:
        return []
    try:
        store = VaultBindingsStore(ctx.local_index.db_path)
        all_bindings = store.list_bindings(vault_id=ctx.vault_id)
    except Exception as exc:  # noqa: BLE001
        ctx.set_content_status(f"Could not load bindings: {exc}", "error")
        return []
    binding_records = [
        b for b in all_bindings
        if b.state != "unbound" and b.remote_folder_id == remote_folder_id
    ]
    try:
        cached = ctx.local_index.list_remote_folders(ctx.vault_id)
    except Exception:  # noqa: BLE001
        cached = []
    names = {
        str(f.get("remote_folder_id", "")):
            str(f.get("display_name_enc") or "")
        for f in cached
    }
    return binding_rows_for_render(
        binding_records, folder_names_by_id=names,
    )


def lookup_folder_settings(
    ctx: FoldersContext, remote_folder_id: str,
) -> tuple[str, list[str]]:
    """Pull the folder's current display name + ignore patterns out
    of the local manifest cache. Returns ("", []) if missing.
    """
    if not ctx.vault_id:
        return "", []
    try:
        cached = ctx.local_index.list_remote_folders(ctx.vault_id)
    except Exception:  # noqa: BLE001
        return "", []
    for f in cached:
        if str(f.get("remote_folder_id") or "") == remote_folder_id:
            return (
                str(f.get("display_name_enc") or ""),
                [str(p) for p in (f.get("ignore_patterns") or [])],
            )
    return "", []


def refresh_folders_usage_async(
    ctx: FoldersContext, message: str | None = None,
) -> None:
    if not ctx.vault_id:
        return
    ctx.set_sidebar_status("Refreshing folder usage…")

    def worker() -> None:
        try:
            manifest = ctx.runtime.fetch_manifest()
            usage = calculate_vault_usage(manifest).by_folder
        except Exception as exc:  # noqa: BLE001
            error_message = humanize(exc)

            def fail() -> bool:
                ctx.set_sidebar_status(
                    f"Folder usage unavailable: {error_message}", "error",
                )
                return False

            GLib.idle_add(fail)
            return

        def succeed() -> bool:
            ctx.usage_by_folder_state["value"] = usage
            ctx.refresh_all(message)
            return False

        GLib.idle_add(succeed)

    threading.Thread(target=worker, daemon=True).start()
