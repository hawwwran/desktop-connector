"""Devices tab — list device grants + revoke (§6.H2).

Replaces the placeholder ``("devices", "Devices")`` entry that used to
live in ``main_window.py``. Card-per-row layout: each row carries the
device's human name (falling back to the truncated device_id), role
badge, last-seen timestamp, and a "Revoke" button. Revoked rows
greyed out + sorted to the bottom; the caller's own row pre-disables
the Revoke button with a tooltip pointing at Danger zone's "Disconnect
this device" surface.

**Revoke gating** (mirrors `tab_danger.py`):

1. Click "Revoke" → fresh-unlock prompt (operator re-types the
   recovery passphrase).
2. Verify ``caller_role == "admin"`` via ``relay.get_header``. Non-admin
   devices see an inline error instead of getting a 403 surprise on
   submit.
3. Type-to-confirm dialog with the §14-locked copy and the dashed
   Vault ID typed into the entry (reuses
   :func:`confirm_vault_clear_text_matches`).
4. ``DELETE /api/vaults/{id}/device-grants/{device_id}`` via the typed
   client in :mod:`desktop.src.vault.grant.client`.
5. On success: refresh the list, surface a green status line.

**Reactive refresh:** initial fetch on tab map; every 30 s while the
tab is visible (silent — no status flicker); unmap clears the timer.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..vault.binding.runtime import (
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault.error_messages import humanize
from ..vault.grant.client import (
    CannotRevokeSelfError,
    DeviceGrant,
    DeviceGrantNotFoundError,
    DeviceGrantsAuthError,
    list_device_grants,
    revoke_device_grant,
)
from ..vault.ops.clear import confirm_vault_clear_text_matches
from ._main_context import MainContext
from .fresh_unlock_prompt import require_fresh_unlock_or_prompt


log = logging.getLogger(__name__)

POLL_INTERVAL_S = 30

# Spec §14 locked wording. Pinned by
# ``test_revoke_dialog_locked_copy_matches_spec_3_3_verbatim`` so a
# future copy edit can't drift the legally-load-bearing line that the
# admin still owns plaintext already on the revoked device.
REVOKE_LOCKED_COPY = (
    "Revoking this device prevents future Vault access. "
    "It cannot erase data already copied to that device."
)


def build_devices_tab(ctx: MainContext, win) -> "Gtk.Box":
    config = ctx.config
    config_dir = ctx.config_dir
    vault_id_undashed = ctx.vault_id_undashed
    vault_id_dashed_fn = ctx.vault_id_dashed

    container = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=12,
        margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
    )

    container.append(Gtk.Label(
        label="Devices", xalign=0, css_classes=["title-3"],
    ))
    container.append(Gtk.Label(
        label=(
            "Every device with a vault grant. Admin can revoke access "
            "from a lost or compromised device — future relay operations "
            "are blocked immediately. Data already downloaded to the "
            "revoked device is not erased."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))

    status_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    container.append(status_label)

    def _set_status(text: str, kind: str = "neutral") -> None:
        status_label.set_label(text)
        status_label.remove_css_class("error")
        status_label.remove_css_class("success")
        if kind == "error":
            status_label.add_css_class("error")
        elif kind == "success":
            status_label.add_css_class("success")

    refresh_btn = Gtk.Button(label="Refresh", css_classes=["pill"])
    refresh_btn.set_halign(Gtk.Align.START)
    container.append(refresh_btn)

    rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    container.append(rows_box)

    state: dict[str, Any] = {
        "loading": False,
        "poll_source_id": None,
        "grants": [],
    }

    def _clear_rows() -> None:
        child = rows_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            rows_box.remove(child)
            child = nxt

    def _render_grants(grants: list[DeviceGrant]) -> None:
        _clear_rows()
        # Sort: active first (newest last_seen first), revoked last
        # (newest revoked_at first). Revoked rows still appear so the
        # admin has an audit trail of who's been kicked out.
        active = sorted(
            [g for g in grants if not g.is_revoked],
            key=lambda g: g.last_seen_at or "",
            reverse=True,
        )
        revoked = sorted(
            [g for g in grants if g.is_revoked],
            key=lambda g: g.revoked_at or "",
            reverse=True,
        )
        for grant in active + revoked:
            rows_box.append(_build_grant_card(grant))

    def _build_grant_card(grant: DeviceGrant) -> "Gtk.Widget":
        card = Gtk.Frame()
        card.add_css_class("card")
        if grant.is_revoked:
            card.add_css_class("dim-label")

        row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )
        card.set_child(row)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        info.append(Gtk.Label(
            label=grant.device_name or grant.device_id[:12],
            xalign=0, css_classes=["heading"],
        ))
        meta_parts = [f"Role: {grant.role or 'unknown'}"]
        if grant.is_caller:
            meta_parts.append("(this device)")
        if grant.last_seen_at:
            meta_parts.append(f"Last seen {grant.last_seen_at}")
        if grant.is_revoked:
            meta_parts.append(f"Revoked {grant.revoked_at or '?'}")
        info.append(Gtk.Label(
            label=" • ".join(meta_parts),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))
        row.append(info)

        if grant.is_revoked:
            badge = Gtk.Label(
                label="Revoked", css_classes=["dim-label"],
            )
            row.append(badge)
        else:
            revoke_btn = Gtk.Button(
                label="Revoke",
                css_classes=["pill", "destructive-action"],
            )
            if grant.is_caller:
                revoke_btn.set_sensitive(False)
                revoke_btn.set_tooltip_text(
                    "Use 'Disconnect this device' in Danger zone "
                    "instead of revoking your own grant.",
                )
            else:
                revoke_btn.connect(
                    "clicked",
                    lambda _b, g=grant: _on_revoke_clicked(g),
                )
            row.append(revoke_btn)

        return card

    def _refresh_list(silent: bool = False) -> None:
        if state["loading"]:
            return
        if not vault_id_undashed:
            _set_status("No vault is connected on this machine.", "error")
            return
        state["loading"] = True
        if not silent:
            refresh_btn.set_sensitive(False)
            _set_status("Loading device grants…")

        def worker() -> None:
            error: Exception | None = None
            grants: list[DeviceGrant] = []
            try:
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    grants = list_device_grants(
                        relay, vault.vault_id, vault.vault_access_secret,
                    )
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                error = exc

            def settle() -> bool:
                state["loading"] = False
                refresh_btn.set_sensitive(True)
                if error is not None:
                    if isinstance(error, DeviceGrantsAuthError):
                        _set_status(
                            "Admin role required to view devices. "
                            "Open Vault Settings on the admin device.",
                            "error",
                        )
                    elif not silent:
                        _set_status(
                            f"Could not load devices: {humanize(error)}",
                            "error",
                        )
                    return False
                state["grants"] = grants
                _render_grants(grants)
                active_count = sum(1 for g in grants if not g.is_revoked)
                revoked_count = len(grants) - active_count
                _set_status(
                    f"{active_count} active grant(s), {revoked_count} revoked.",
                )
                return False

            GLib.idle_add(settle)

        threading.Thread(target=worker, daemon=True).start()

    def _on_revoke_clicked(grant: DeviceGrant) -> None:
        target_label = grant.device_name or grant.device_id[:12]

        def after_fresh_unlock() -> None:
            _verify_admin_then_open_dialog(grant)

        def cancelled() -> None:
            _set_status(f"Revoke of {target_label!r} cancelled.")

        require_fresh_unlock_or_prompt(
            win,
            config=config,
            operation_label=f"revoke device {target_label!r}",
            on_success=after_fresh_unlock,
            on_cancel=cancelled,
        )

    def _verify_admin_then_open_dialog(grant: DeviceGrant) -> None:
        _set_status("Checking admin role…")
        refresh_btn.set_sensitive(False)

        def role_worker() -> None:
            role: str | None = None
            err: Exception | None = None
            try:
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    header = relay.get_header(
                        vault.vault_id, vault.vault_access_secret,
                    )
                    raw = header.get("caller_role")
                    role = str(raw) if raw else None
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                err = exc

            def settle() -> bool:
                refresh_btn.set_sensitive(True)
                if err is not None:
                    _set_status(
                        f"Could not verify device role: {humanize(err)}",
                        "error",
                    )
                    return False
                if role != "admin":
                    _set_status(
                        f"Revoke requires admin role. This device's role "
                        f"is {role!r}. Use the admin device that created "
                        "the vault.",
                        "error",
                    )
                    return False
                _set_status("")
                _open_revoke_confirm_dialog(grant)
                return False

            GLib.idle_add(settle)

        threading.Thread(target=role_worker, daemon=True).start()

    def _open_revoke_confirm_dialog(grant: DeviceGrant) -> None:
        expected = vault_id_dashed_fn()
        target_label = grant.device_name or grant.device_id[:12]

        dlg = Adw.AlertDialog(
            heading=f"Revoke device {target_label!r}?",
            body=(
                f"{REVOKE_LOCKED_COPY}\n\n"
                f"Type the full Vault ID ({expected}) to confirm."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("revoke", f"Revoke {target_label!r}")
        dlg.set_response_appearance("revoke", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_enabled("revoke", False)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        entry = Gtk.Entry(placeholder_text=expected)

        def on_typed(_e) -> None:
            ok = confirm_vault_clear_text_matches(entry.get_text(), expected)
            dlg.set_response_enabled("revoke", ok)

        entry.connect("changed", on_typed)
        dlg.set_extra_child(entry)

        def on_resp(_d, response: str) -> None:
            if response != "revoke":
                return
            _do_revoke(grant)

        dlg.connect("response", on_resp)
        dlg.present(win)

    def _do_revoke(grant: DeviceGrant) -> None:
        target_label = grant.device_name or grant.device_id[:12]
        _set_status(f"Revoking {target_label!r}…")
        refresh_btn.set_sensitive(False)

        def worker() -> None:
            err: Exception | None = None
            try:
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    revoke_device_grant(
                        relay, vault.vault_id, vault.vault_access_secret,
                        grant.device_id,
                    )
                    log.info(
                        "vault.device.revoked device_id=%s",
                        grant.device_id[:12],
                    )
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                err = exc

            def settle() -> bool:
                refresh_btn.set_sensitive(True)
                if err is not None:
                    if isinstance(err, CannotRevokeSelfError):
                        msg = (
                            "Cannot revoke your own grant. Use "
                            "'Disconnect this device' in Danger zone."
                        )
                    elif isinstance(err, DeviceGrantNotFoundError):
                        msg = "Device grant already removed."
                    elif isinstance(err, DeviceGrantsAuthError):
                        msg = "Admin role required to revoke devices."
                    else:
                        msg = f"Revoke failed: {humanize(err)}"
                    _set_status(msg, "error")
                    return False
                _set_status(
                    f"Revoked {target_label!r}. "
                    "Future relay operations are blocked.",
                    "success",
                )
                _refresh_list()
                return False

            GLib.idle_add(settle)

        threading.Thread(target=worker, daemon=True).start()

    refresh_btn.connect("clicked", lambda _b: _refresh_list())

    # Initial fetch on map + silent 30 s poll while visible.
    def _on_map(_w) -> None:
        _refresh_list()
        if state["poll_source_id"] is None:
            state["poll_source_id"] = GLib.timeout_add_seconds(
                POLL_INTERVAL_S,
                lambda: (_refresh_list(silent=True) or True),
            )

    def _on_unmap(_w) -> None:
        if state["poll_source_id"] is not None:
            GLib.source_remove(state["poll_source_id"])
            state["poll_source_id"] = None

    container.connect("map", _on_map)
    container.connect("unmap", _on_unmap)

    return container
