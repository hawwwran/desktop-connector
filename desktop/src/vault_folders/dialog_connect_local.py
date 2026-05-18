"""Connect-local-folder dialog + initial-baseline runner.

When invoked from a per-folder card we narrow the dropdown to that
folder; otherwise we expose the whole list. The dialog itself comes
from :mod:`vault_connect_folder_dialog` — this module just handles the
worker thread that fetches the manifest, wires up the
``on_dialog_confirmed`` callback, and runs the initial baseline in a
second worker thread once the user confirms.
"""

from __future__ import annotations

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from ..vault.binding.bindings import VaultBindingsStore
from ..vault.folder.connect_dialog import present_connect_folder_dialog
from ..vault.folder.runtime import VaultBaselineHeadMovedError
from ..vault.error_messages import humanize
from .context import FoldersContext


def open_connect_local_dialog(
    ctx: FoldersContext, remote_folder_id: str | None = None,
) -> None:
    if not ctx.vault_id:
        return

    def worker() -> None:
        try:
            manifest = ctx.runtime.fetch_manifest()
        except Exception as exc:  # noqa: BLE001
            error_message = humanize(exc)

            def fail() -> bool:
                ctx.set_content_status(
                    f"Could not load manifest for connect: "
                    f"{error_message}", "error",
                )
                return False

            GLib.idle_add(fail)
            return

        def show() -> bool:
            all_choices = [
                (str(f.get("display_name_enc", "")),
                 str(f.get("remote_folder_id", "")))
                for f in manifest.get("remote_folders", []) or []
                if isinstance(f, dict)
                and str(f.get("state", "active")) == "active"
            ]
            if not all_choices:
                ctx.set_content_status(
                    "No remote folders yet — create one before "
                    "connecting a local folder.", "error",
                )
                return False
            # When invoked from a per-folder card we narrow the
            # dropdown to that folder; otherwise we expose the
            # whole list.
            if remote_folder_id:
                choices = [
                    c for c in all_choices if c[1] == remote_folder_id
                ]
                if not choices:
                    choices = all_choices
            else:
                choices = all_choices

            store = VaultBindingsStore(ctx.local_index.db_path)

            # Review §3.H6: capture the preflight head revision so the
            # baseline run can refuse to fire if the relay's head
            # has advanced between dialog open and user-Confirm. The
            # baseline-side check lives in
            # ``VaultRuntime.run_initial_baseline``.
            preflight_revision = int(manifest.get("revision", 0))

            def on_dialog_confirmed(record) -> None:
                ctx.set_content_status(
                    "Binding created — running initial baseline…",
                )
                ctx.selection_state["folder_id"] = record.remote_folder_id
                ctx.refresh_all()
                threading.Thread(
                    target=lambda: _run_baseline_for_record(record, preflight_revision),
                    daemon=True,
                ).start()

            def _run_baseline_for_record(record, expected_revision) -> None:
                try:
                    ctx.runtime.run_initial_baseline(
                        record=record,
                        expected_root_revision=expected_revision,
                    )
                except VaultBaselineHeadMovedError as exc:
                    def re_preflight() -> bool:
                        ctx.set_content_status(
                            "Another device published while you were "
                            "choosing the local folder. Re-open the "
                            "Connect dialog so the preflight numbers "
                            "match the new server head "
                            f"(was rev {exc.expected_revision}, now "
                            f"rev {exc.observed_revision}).",
                            "error",
                        )
                        ctx.refresh_all()
                        return False

                    GLib.idle_add(re_preflight)
                    return
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)

                    def fail() -> bool:
                        ctx.set_content_status(
                            f"Initial baseline failed: {msg}", "error",
                        )
                        ctx.refresh_all()
                        return False

                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    ctx.set_content_status(
                        "Binding ready — initial baseline complete.",
                        "success",
                    )
                    ctx.refresh_all()
                    return False

                GLib.idle_add(succeed)

            present_connect_folder_dialog(
                parent_window=ctx.parent_window,
                folder_choices=choices,
                manifest=manifest,
                vault_id=ctx.vault_id,
                store=store,
                on_confirmed=on_dialog_confirmed,
            )
            return False

        GLib.idle_add(show)

    threading.Thread(target=worker, daemon=True).start()
