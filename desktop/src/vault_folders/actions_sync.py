"""Per-binding sync action runners (Sync now / Pause / Resume).

Each helper spawns a worker thread, runs the relevant
:mod:`vault_folder_actions` dispatcher (or, for Sync now / Resume, the
shared :meth:`VaultRuntime.flush_and_sync_binding` plumbing), and
marshals the result back onto the GTK main loop with ``GLib.idle_add``.
"""

from __future__ import annotations

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib  # noqa: E402

from ..vault_binding_sync import format_sync_outcome_toast
from ..vault_bindings import VaultBindingsStore
from ..vault.error_messages import humanize
from ..vault_folder_actions import dispatch_pause, dispatch_resume
from .context import FoldersContext


def _idle_finish(
    ctx: FoldersContext,
    toast: str | None,
    error: str | None,
    prefix: str,
) -> None:
    def apply() -> bool:
        if error:
            ctx.set_content_status(error, "error")
        else:
            ctx.set_content_status(toast or f"{prefix} done.", "success")
        ctx.refresh_all()
        return False

    GLib.idle_add(apply)


def run_sync_now(
    ctx: FoldersContext, binding_id: str, button: Gtk.Button,
) -> None:
    if ctx.sync_in_flight.get(binding_id):
        return
    ctx.sync_in_flight[binding_id] = True
    button.set_sensitive(False)
    ctx.set_content_status("Sync now: running…")

    def worker() -> None:
        try:
            author_device_id = ctx.config.device_id or ("0" * 32)
            device_name = (
                str(ctx.config.device_name or "").strip() or "this device"
            )
            event = ctx.cancellation_registry.register(binding_id)
            try:
                result = ctx.runtime.flush_and_sync_binding(
                    binding_id=binding_id,
                    author_device_id=author_device_id,
                    device_name=device_name,
                    should_continue=lambda: not event.is_set(),
                )
            finally:
                ctx.cancellation_registry.clear(binding_id)
            toast_text = format_sync_outcome_toast(result)
        except Exception as exc:  # noqa: BLE001
            error_message = humanize(exc)

            def fail() -> bool:
                ctx.sync_in_flight[binding_id] = False
                button.set_sensitive(True)
                ctx.set_content_status(
                    f"Sync now failed: {error_message}", "error",
                )
                return False

            GLib.idle_add(fail)
            return

        def succeed() -> bool:
            ctx.sync_in_flight[binding_id] = False
            button.set_sensitive(True)
            ctx.set_content_status(toast_text, "success")
            ctx.refresh_all()
            return False

        GLib.idle_add(succeed)

    threading.Thread(target=worker, daemon=True).start()


def run_pause(ctx: FoldersContext, binding_id: str) -> None:
    if ctx.action_in_flight.get(binding_id):
        return
    ctx.action_in_flight[binding_id] = True
    ctx.set_content_status("Pause: running…")

    def worker() -> None:
        try:
            store = VaultBindingsStore(ctx.local_index.db_path)
            toast, error = dispatch_pause(
                store=store, binding_id=binding_id,
                cancellation=ctx.cancellation_registry,
            )
            _idle_finish(ctx, toast, error, "Pause")
        finally:
            ctx.action_in_flight[binding_id] = False

    threading.Thread(target=worker, daemon=True).start()


def run_resume(ctx: FoldersContext, binding_id: str) -> None:
    if ctx.action_in_flight.get(binding_id):
        return
    ctx.action_in_flight[binding_id] = True
    ctx.set_content_status("Resume: running…")

    def worker() -> None:
        try:
            store = VaultBindingsStore(ctx.local_index.db_path)

            def flush(_binding) -> object:
                # Resume reuses the Sync-now plumbing — same vault
                # open, same registry registration, same
                # should_continue gate so a fresh Pause arriving
                # during the post-resume flush still aborts within
                # ~1 chunk.
                event = ctx.cancellation_registry.register(binding_id)
                try:
                    return ctx.runtime.flush_and_sync_binding(
                        binding_id=binding_id,
                        author_device_id=ctx.config.device_id or ("0" * 32),
                        device_name=(
                            str(ctx.config.device_name or "").strip()
                            or "this device"
                        ),
                        should_continue=lambda: not event.is_set(),
                    )
                finally:
                    ctx.cancellation_registry.clear(binding_id)

            toast, error = dispatch_resume(
                store=store, binding_id=binding_id, flush=flush,
            )
            _idle_finish(ctx, toast, error, "Resume")
        finally:
            ctx.action_in_flight[binding_id] = False

    threading.Thread(target=worker, daemon=True).start()
