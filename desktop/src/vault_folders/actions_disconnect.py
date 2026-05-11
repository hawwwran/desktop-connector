"""Disconnect-binding action runner (with confirmation dialog).

Splits the per-binding ``run_disconnect`` closure out of the original
``build_vault_folders_tab``. The worker thread waits on a
``threading.Condition`` while the GTK main loop renders the confirm
dialog so the worker only proceeds once the user has accepted /
cancelled.
"""

from __future__ import annotations

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from ..vault_bindings import VaultBindingsStore
from ..vault.folder.actions import dispatch_disconnect
from .actions_sync import _idle_finish
from .context import FoldersContext


def run_disconnect(ctx: FoldersContext, binding_id: str) -> None:
    if ctx.action_in_flight.get(binding_id):
        return
    ctx.action_in_flight[binding_id] = True
    ctx.set_content_status("Disconnect: confirming…")

    decision: dict[str, bool] = {}
    cv = threading.Condition()

    def show_dialog() -> bool:
        dialog = Adw.AlertDialog(
            heading="Disconnect this folder?",
            body=(
                "Pending sync operations for this binding will be "
                "dropped. Local files and the remote vault are not "
                "touched. You can re-connect the folder later."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("disconnect", "Disconnect")
        dialog.set_response_appearance(
            "disconnect", Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d, response: str) -> None:
            with cv:
                decision["confirmed"] = (response == "disconnect")
                cv.notify_all()

        dialog.connect("response", on_response)
        dialog.present(ctx.parent_window)
        return False

    GLib.idle_add(show_dialog)

    def worker() -> None:
        try:
            with cv:
                while "confirmed" not in decision:
                    cv.wait()
            confirmed = decision["confirmed"]
            store = VaultBindingsStore(ctx.local_index.db_path)
            toast, error = dispatch_disconnect(
                store=store, binding_id=binding_id,
                confirm=lambda: confirmed,
                cancellation=ctx.cancellation_registry,
            )
            _idle_finish(ctx, toast, error, "Disconnect")
        finally:
            ctx.action_in_flight[binding_id] = False

    threading.Thread(target=worker, daemon=True).start()
