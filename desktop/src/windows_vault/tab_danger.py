"""Danger zone — disconnect / clear folder / clear vault / schedule purge.

Extracted from ``windows_vault.py`` (lines ~1094–1580).
"""

import threading
from datetime import datetime, timezone

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from ..vault.ops.clear import (
    build_clear_folder_manifest,
    build_clear_vault_manifest,
    confirm_folder_clear_text_matches,
    confirm_vault_clear_text_matches,
)
from ..vault.error_messages import humanize
from .fresh_unlock_prompt import require_fresh_unlock_or_prompt
from ..vault.ops.purge_schedule import (
    DEFAULT_DELAY_SECONDS,
    PendingPurge,
    VaultPurgeAlreadyScheduledError,
    VaultPurgeError,
    cancel_purge,
    get_pending_purge,
    schedule_purge,
)
from ._main_context import MainContext


def build_danger_tab(ctx: MainContext, win) -> "Gtk.Box":
    config = ctx.config
    config_dir = ctx.config_dir
    vault_id_undashed = ctx.vault_id_undashed
    vault_id_dashed = ctx.vault_id_dashed

    danger = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=12,
        margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
    )
    danger.append(Gtk.Label(label="Disconnect vault", xalign=0, css_classes=["title-3"]))
    danger.append(Gtk.Label(
        label="Remove this machine's local connection to the vault.",
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))
    disconnect_btn = Gtk.Button(label="Disconnect vault", css_classes=["pill", "destructive-action"])
    disconnect_btn.set_halign(Gtk.Align.START)
    disconnect_btn.set_sensitive(bool(vault_id_undashed))
    danger.append(disconnect_btn)

    def on_disconnect_vault(_btn):
        dlg = Adw.AlertDialog(
            heading="Disconnect vault?",
            # F-U18: spell out exactly what disconnect removes vs.
            # leaves alone — "vault will still exist" was technically
            # true but underplayed the local data wipe.
            body=(
                "Removes all local vault material from this machine "
                "(keys, manifests, downloaded chunks, sync state). "
                "The relay vault is untouched. To reconnect, ask an "
                "admin device to grant access again."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("disconnect", "Disconnect vault")
        dlg.set_response_appearance("disconnect", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_resp(_dialog, response):
            if response != "disconnect":
                return
            from ..vault.state.local_state import disconnect_local_vault
            disconnect_local_vault(config)
            win.close()

        dlg.connect("response", on_resp)
        dlg.present(win)

    disconnect_btn.connect("clicked", on_disconnect_vault)

    # ---------------------------------------------------------------
    # F-U22: Clear folder / Clear whole vault / Schedule hard purge
    # Each gated behind a typed-confirmation dialog (§gaps §13).
    # Backend pure-functions: vault_clear.{build_clear_folder_manifest,
    # build_clear_vault_manifest, confirm_*_text_matches} +
    # vault_purge_schedule.{schedule_purge, cancel_purge}.
    # ---------------------------------------------------------------

    danger_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    danger.append(danger_status)

    def _set_danger_status(text: str, kind: str = "neutral") -> None:
        danger_status.set_label(text)
        danger_status.remove_css_class("error")
        danger_status.remove_css_class("success")
        if kind == "error":
            danger_status.add_css_class("error")
        elif kind == "success":
            danger_status.add_css_class("success")

    # ----- Clear folder ---------------------------------------------

    danger.append(Gtk.Separator(margin_top=12, margin_bottom=12))
    danger.append(Gtk.Label(
        label="Clear folder", xalign=0, css_classes=["title-3"],
    ))
    danger.append(Gtk.Label(
        label=(
            "Tombstones every active file in one folder. Files remain "
            "in version history until eviction reclaims them; the "
            "folder itself stays bound. Type the folder name to confirm."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))
    clear_folder_row = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
    )
    clear_folder_combo = Gtk.DropDown.new_from_strings([])
    clear_folder_combo.set_hexpand(True)
    clear_folder_btn = Gtk.Button(
        label="Clear folder…",
        css_classes=["pill", "destructive-action"],
    )
    clear_folder_row.append(clear_folder_combo)
    clear_folder_row.append(clear_folder_btn)
    danger.append(clear_folder_row)

    # State holder for the dropdown — list of (display_name, id) tuples.
    clear_folder_state: dict[str, list] = {"folders": []}

    def _refresh_clear_folder_options() -> None:
        """Rebuild the folder dropdown from the local vault manifest cache."""
        try:
            from ..vault.state.local_index import VaultLocalIndex
            local_index = VaultLocalIndex(config_dir)
            folders = (
                local_index.list_remote_folders(vault_id_undashed)
                if vault_id_undashed else []
            )
        except Exception:  # noqa: BLE001
            folders = []
        entries = []
        for folder in folders:
            fid = str(folder.get("remote_folder_id", ""))
            name = str(folder.get("display_name_enc") or fid)
            if fid:
                entries.append((name, fid))
        clear_folder_state["folders"] = entries
        model = Gtk.StringList.new([e[0] for e in entries] or [
            "(no folders)" if vault_id_undashed else "(no vault)"
        ])
        clear_folder_combo.set_model(model)
        clear_folder_btn.set_sensitive(bool(entries))

    _refresh_clear_folder_options()

    def on_clear_folder(_btn) -> None:
        entries = clear_folder_state["folders"]
        idx = clear_folder_combo.get_selected()
        if idx is None or idx >= len(entries):
            return
        display_name, folder_id = entries[idx]

        def proceed() -> None:
            _open_clear_folder_dialog(display_name, folder_id)

        require_fresh_unlock_or_prompt(
            win,
            config=config,
            operation_label=f"clear folder {display_name!r}",
            on_success=proceed,
        )

    def _open_clear_folder_dialog(display_name: str, folder_id: str) -> None:
        dlg = Adw.AlertDialog(
            heading=f"Clear folder {display_name!r}?",
            body=(
                "⚠ Every active file in this folder will be tombstoned. "
                "Files remain in version history until eviction reclaims "
                "them; the folder binding itself stays connected. "
                f"Type the folder name ({display_name!r}) to confirm."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("clear", f"Clear folder {display_name!r}")
        dlg.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_enabled("clear", False)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        confirm_entry = Gtk.Entry(placeholder_text=display_name)

        def on_typed(_entry) -> None:
            ok = confirm_folder_clear_text_matches(
                confirm_entry.get_text(), display_name,
            )
            dlg.set_response_enabled("clear", ok)
        confirm_entry.connect("changed", on_typed)
        dlg.set_extra_child(confirm_entry)

        def on_resp(_dialog, response: str) -> None:
            if response != "clear":
                return
            _do_clear_folder(folder_id, display_name)

        dlg.connect("response", on_resp)
        dlg.present(win)

    def _do_clear_folder(folder_id: str, display_name: str) -> None:
        clear_folder_btn.set_sensitive(False)
        _set_danger_status(f"Clearing folder {display_name!r}…")

        def worker() -> None:
            try:
                from ..vault.binding.runtime import (
                    create_vault_relay, open_local_vault_from_grant,
                )
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    manifest = vault.fetch_manifest(relay)
                    device_id = config.device_id or ("0" * 32)
                    deleted_at = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )
                    next_manifest = build_clear_folder_manifest(
                        manifest,
                        remote_folder_id=folder_id,
                        author_device_id=device_id,
                        deleted_at=deleted_at,
                    )
                    vault.publish_manifest(relay, next_manifest)
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                msg = humanize(exc)

                def fail() -> bool:
                    clear_folder_btn.set_sensitive(True)
                    _set_danger_status(
                        f"Clear folder failed: {msg}", "error",
                    )
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                clear_folder_btn.set_sensitive(True)
                _set_danger_status(
                    f"Folder {display_name!r} cleared. "
                    "Reclaim space via Maintenance / eviction.",
                    "success",
                )
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    clear_folder_btn.connect("clicked", on_clear_folder)

    # ----- Clear whole vault ---------------------------------------

    danger.append(Gtk.Separator(margin_top=12, margin_bottom=12))
    danger.append(Gtk.Label(
        label="Clear whole vault", xalign=0, css_classes=["title-3"],
    ))
    danger.append(Gtk.Label(
        label=(
            "Tombstones every active file across every folder in this "
            "vault. Files remain recoverable from version history until "
            "eviction reclaims them. Permanent only after a hard purge "
            "(see below). Type the full Vault ID to confirm."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))
    clear_vault_btn = Gtk.Button(
        label="Clear whole vault…",
        css_classes=["pill", "destructive-action"],
    )
    clear_vault_btn.set_halign(Gtk.Align.START)
    clear_vault_btn.set_sensitive(bool(vault_id_undashed))
    danger.append(clear_vault_btn)

    def on_clear_vault(_btn) -> None:
        def proceed() -> None:
            _open_clear_vault_dialog()

        require_fresh_unlock_or_prompt(
            win,
            config=config,
            operation_label="clear whole vault",
            on_success=proceed,
        )

    def _open_clear_vault_dialog() -> None:
        expected = vault_id_dashed()
        dlg = Adw.AlertDialog(
            heading="Clear whole vault?",
            body=(
                "⚠ Every active file across every folder in this vault "
                "will be tombstoned. Files stay in version history until "
                "eviction reclaims them. Type the full Vault ID "
                f"({expected}) to confirm."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("clear", "Clear whole vault")
        dlg.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_enabled("clear", False)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        confirm_entry = Gtk.Entry(placeholder_text=expected)

        def on_typed(_entry) -> None:
            ok = confirm_vault_clear_text_matches(
                confirm_entry.get_text(), expected,
            )
            dlg.set_response_enabled("clear", ok)
        confirm_entry.connect("changed", on_typed)
        dlg.set_extra_child(confirm_entry)

        def on_resp(_dialog, response: str) -> None:
            if response != "clear":
                return
            _do_clear_vault()
        dlg.connect("response", on_resp)
        dlg.present(win)

    def _do_clear_vault() -> None:
        clear_vault_btn.set_sensitive(False)
        _set_danger_status("Clearing whole vault…")

        def worker() -> None:
            try:
                from ..vault.binding.runtime import (
                    create_vault_relay, open_local_vault_from_grant,
                )
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    manifest = vault.fetch_manifest(relay)
                    device_id = config.device_id or ("0" * 32)
                    deleted_at = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )
                    next_manifest = build_clear_vault_manifest(
                        manifest,
                        author_device_id=device_id,
                        deleted_at=deleted_at,
                    )
                    vault.publish_manifest(relay, next_manifest)
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                msg = humanize(exc)

                def fail() -> bool:
                    clear_vault_btn.set_sensitive(True)
                    _set_danger_status(
                        f"Clear vault failed: {msg}", "error",
                    )
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                clear_vault_btn.set_sensitive(True)
                _set_danger_status(
                    "Vault cleared. Files remain in version history "
                    "until eviction reclaims them.",
                    "success",
                )
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()

    clear_vault_btn.connect("clicked", on_clear_vault)

    # ----- Schedule hard purge -------------------------------------

    danger.append(Gtk.Separator(margin_top=12, margin_bottom=12))
    danger.append(Gtk.Label(
        label="Schedule hard purge", xalign=0, css_classes=["title-3"],
    ))
    danger.append(Gtk.Label(
        label=(
            "⚠ Permanent. After the delay elapses, all chunk + manifest "
            "data for this vault is deleted from the relay. Even with the "
            "recovery kit the vault becomes unrecoverable. Type the full "
            "Vault ID to confirm."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))
    purge_row = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
    )
    purge_row.append(Gtk.Label(label="Delay (hours):", xalign=0))
    purge_delay_entry = Gtk.Entry(
        text=str(DEFAULT_DELAY_SECONDS // 3600),
        max_length=4,
    )
    purge_delay_entry.set_size_request(80, -1)
    purge_row.append(purge_delay_entry)
    purge_btn = Gtk.Button(
        label="Schedule hard purge…",
        css_classes=["pill", "destructive-action"],
    )
    purge_row.append(purge_btn)
    danger.append(purge_row)

    purge_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    danger.append(purge_status)
    cancel_purge_btn = Gtk.Button(
        label="Cancel scheduled purge", css_classes=["pill"],
    )
    cancel_purge_btn.set_halign(Gtk.Align.START)
    cancel_purge_btn.set_visible(False)
    danger.append(cancel_purge_btn)

    def _refresh_purge_status() -> None:
        if not vault_id_undashed:
            purge_status.set_label("")
            purge_btn.set_sensitive(False)
            cancel_purge_btn.set_visible(False)
            return
        existing = get_pending_purge(config_dir, vault_id_dashed())
        if existing is None:
            purge_status.set_label("No pending purge.")
            purge_btn.set_sensitive(True)
            cancel_purge_btn.set_visible(False)
            return
        when = datetime.fromtimestamp(
            existing.scheduled_for_epoch, tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M UTC")
        purge_status.set_label(
            f"Hard purge scheduled for {when} (job_id={existing.job_id}). "
            "Cancel below to abort."
        )
        purge_btn.set_sensitive(False)
        cancel_purge_btn.set_visible(True)

    _refresh_purge_status()

    def on_schedule_purge(_btn) -> None:
        try:
            hours = int(purge_delay_entry.get_text().strip() or "0")
        except ValueError:
            _set_danger_status(
                "Delay must be a whole number of hours.", "error",
            )
            return
        if hours < 0:
            _set_danger_status(
                "Delay must be non-negative.", "error",
            )
            return

        def proceed() -> None:
            _open_schedule_purge_dialog(hours)

        require_fresh_unlock_or_prompt(
            win,
            config=config,
            operation_label=f"schedule hard purge ({hours}h delay)",
            on_success=proceed,
        )

    def _open_schedule_purge_dialog(hours: int) -> None:
        delay_seconds = hours * 3600
        expected = vault_id_dashed()

        dlg = Adw.AlertDialog(
            heading="Schedule hard purge?",
            body=(
                f"⚠ Permanent. After {hours} hour(s), every chunk and "
                "manifest in this vault is deleted from the relay. "
                "The recovery kit cannot restore the vault after this "
                f"point. Type the full Vault ID ({expected}) to confirm."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("schedule", "Schedule hard purge")
        dlg.set_response_appearance(
            "schedule", Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dlg.set_response_enabled("schedule", False)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        confirm_entry = Gtk.Entry(placeholder_text=expected)

        def on_typed(_entry) -> None:
            ok = confirm_vault_clear_text_matches(
                confirm_entry.get_text(), expected,
            )
            dlg.set_response_enabled("schedule", ok)
        confirm_entry.connect("changed", on_typed)
        dlg.set_extra_child(confirm_entry)

        def on_resp(_dialog, response: str) -> None:
            if response != "schedule":
                return
            try:
                schedule_purge(
                    config_dir,
                    vault_id_dashed=expected,
                    scope="vault",
                    scope_target=None,
                    scheduled_by_device_id=config.device_id or ("0" * 32),
                    delay_seconds=delay_seconds,
                )
            except VaultPurgeAlreadyScheduledError as exc:
                _set_danger_status(
                    f"Already scheduled — cancel first. ({exc})",
                    "error",
                )
                _refresh_purge_status()
                return
            except VaultPurgeError as exc:
                _set_danger_status(
                    f"Schedule failed: {exc}", "error",
                )
                return
            _set_danger_status(
                f"Hard purge scheduled in {hours} hour(s).",
                "success",
            )
            _refresh_purge_status()

        dlg.connect("response", on_resp)
        dlg.present(win)

    purge_btn.connect("clicked", on_schedule_purge)

    def on_cancel_purge(_btn) -> None:
        cleared = cancel_purge(config_dir, vault_id_dashed())
        if cleared is None:
            _set_danger_status("No pending purge to cancel.")
        else:
            _set_danger_status(
                f"Pending purge cancelled (job_id={cleared.job_id}).",
                "success",
            )
        _refresh_purge_status()

    cancel_purge_btn.connect("clicked", on_cancel_purge)

    return danger
