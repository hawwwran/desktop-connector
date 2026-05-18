"""Claimant-side QR-join wizard (§5.C2).

Subprocess invoked from the tray menu's "Add this device to a vault…"
entry (visible when the vault toggle is on but no local vault exists
yet). Walks the two-step paste-URL flow:

1. **Paste URL** — operator pastes the ``vault://...`` join URL from
   the admin device's screen. We parse + validate, generate a fresh
   X25519 keypair, POST ``claim`` to take ownership of the
   join-request, and compute the 6-digit verification code locally.
2. **Verify + wait** — display the verification code prominently and
   poll ``GET /api/vaults/{id}/join-requests/{req_id}`` every 2 s
   until ``state="approved"``. On approval, AEAD-unwrap the carried
   grant and save it to this device's keyring/file store + record
   ``last_known_id`` in ``config.json``. On reject / expire / cancel,
   surface a typed error and offer Retry.

Webcam QR scanning is intentionally deferred to a v1.x follow-up —
``vault://`` paste is the v1 path, simpler to ship and works on
headless desktops without portal/Wayland complexity.

Limitations of the claimed-via-QR device path (vs created-locally):

- The claimant doesn't have ``recovery_envelope_meta`` because they
  never typed the passphrase — :mod:`vault.fresh_unlock` will refuse
  sensitive ops on this device. The user can later run the Import
  wizard with the recovery kit to upgrade this device to a "full"
  unlocked state.
"""

from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .windows_common import _make_app


log = logging.getLogger(__name__)


POLL_INTERVAL_S = 2


def show_vault_join(config_dir: Path) -> None:
    """Top-level entry point for the ``vault-join`` subprocess."""
    from .config import Config
    from .vault.grant.qr import (
        VaultGrantQRError,
        VaultJoinUrl,
        derive_shared_secret,
        derive_verification_code,
        parse_join_url,
    )
    from .vault.grant.join_client import (
        JoinRequest,
        JoinRequestAuthError,
        JoinRequestError,
        JoinRequestNotFoundError,
        JoinRequestRateLimitedError,
        JoinRequestStateError,
        claim_join_request,
        get_join_request,
    )
    from .vault.grant.wrap import GrantWrapError, unwrap_grant_for_claimant

    config = Config(config_dir)
    app = _make_app()

    state: dict = {
        "step": "paste_url",  # paste_url → verifying → success | error
        "claimant_priv": None,
        "claimant_pub": None,
        "join_url": None,                # VaultJoinUrl dataclass
        "verification_code": None,
        "shared_secret": None,
        "poll_source_id": None,
        "cancelled": threading.Event(),
    }

    def on_activate(_app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)

        win = Adw.ApplicationWindow(
            application=app,
            title="Add this device to a vault",
            default_width=560,
            default_height=420,
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        win.set_content(toolbar)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            margin_top=20, margin_bottom=20, margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_hexpand(True)
        stack.set_vexpand(True)
        outer.append(stack)

        # ----- Step 1: paste URL ------------------------------------
        paste_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        paste_page.append(Gtk.Label(
            label="Add this device to a vault",
            xalign=0, css_classes=["title-2"],
        ))
        paste_page.append(Gtk.Label(
            label=(
                "On the admin device, open Vault Settings → Devices → "
                "Grant a new device, then paste the join URL below. "
                "The URL is short-lived (≤ 15 min) and tied to one "
                "join attempt."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        url_entry = Gtk.Entry(placeholder_text="vault://relay-host/XXXX-XXXX-XXXX/jr_v1_…")
        url_entry.set_hexpand(True)
        paste_page.append(url_entry)

        device_name_entry = Gtk.Entry(
            placeholder_text="This device's name (shown on the admin device)",
        )
        device_name_entry.set_text(_default_device_name())
        device_name_entry.set_hexpand(True)
        paste_page.append(device_name_entry)

        paste_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        paste_page.append(paste_status)

        paste_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END,
        )
        cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
        cancel_btn.connect("clicked", lambda _b: win.close())
        paste_actions.append(cancel_btn)
        claim_btn = Gtk.Button(
            label="Continue", css_classes=["pill", "suggested-action"],
        )
        paste_actions.append(claim_btn)
        paste_page.append(paste_actions)

        stack.add_named(paste_page, "paste_url")

        # ----- Step 2: verify code + wait ---------------------------
        verify_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        verify_page.append(Gtk.Label(
            label="Verify with the admin device",
            xalign=0, css_classes=["title-2"],
        ))
        verify_page.append(Gtk.Label(
            label=(
                "Compare this 6-digit code with the code shown on the "
                "admin device. If they match, the connection is secure; "
                "ask the admin to approve from their device."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        code_label = Gtk.Label(
            label="•••-•••",
            xalign=0.5,
            css_classes=["title-1", "monospace"],
        )
        code_label.set_hexpand(True)
        verify_page.append(code_label)

        verify_status = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
            label="Waiting for the admin device to approve…",
        )
        verify_page.append(verify_status)

        verify_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END,
        )
        verify_cancel = Gtk.Button(label="Cancel", css_classes=["pill"])
        verify_cancel.connect("clicked", lambda _b: win.close())
        verify_actions.append(verify_cancel)
        verify_page.append(verify_actions)

        stack.add_named(verify_page, "verifying")

        # ----- Step 3: success --------------------------------------
        success_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        success_page.append(Gtk.Label(
            label="Vault unlocked on this device",
            xalign=0, css_classes=["title-2"],
        ))
        success_body = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        success_page.append(success_body)
        success_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END,
        )
        success_close = Gtk.Button(
            label="Close", css_classes=["pill", "suggested-action"],
        )
        success_close.connect("clicked", lambda _b: win.close())
        success_actions.append(success_close)
        success_page.append(success_actions)
        stack.add_named(success_page, "success")

        # --- handlers -----------------------------------------------
        def _set_paste_status(text: str, kind: str = "neutral") -> None:
            paste_status.set_label(text)
            paste_status.remove_css_class("error")
            paste_status.remove_css_class("success")
            if kind == "error":
                paste_status.add_css_class("error")
            elif kind == "success":
                paste_status.add_css_class("success")

        def _set_verify_status(text: str, kind: str = "neutral") -> None:
            verify_status.set_label(text)
            verify_status.remove_css_class("error")
            verify_status.remove_css_class("success")
            if kind == "error":
                verify_status.add_css_class("error")
            elif kind == "success":
                verify_status.add_css_class("success")

        def on_claim_clicked(_btn) -> None:
            url_text = url_entry.get_text().strip()
            if not url_text:
                _set_paste_status("Paste a join URL to continue.", "error")
                return

            try:
                join_url: VaultJoinUrl = parse_join_url(url_text)
            except VaultGrantQRError as exc:
                _set_paste_status(f"Invalid join URL: {exc}", "error")
                return

            if join_url.is_expired():
                _set_paste_status(
                    "This join URL has expired. Ask the admin to "
                    "generate a fresh one (≤ 15 min lifetime).",
                    "error",
                )
                return

            from nacl.bindings import crypto_scalarmult_base
            from secrets import token_bytes

            claimant_priv = token_bytes(32)
            claimant_pub = crypto_scalarmult_base(claimant_priv)
            shared = derive_shared_secret(claimant_priv, join_url.ephemeral_pubkey)
            code = derive_verification_code(shared)

            state["claimant_priv"] = claimant_priv
            state["claimant_pub"] = claimant_pub
            state["join_url"] = join_url
            state["shared_secret"] = shared
            state["verification_code"] = code

            device_name = device_name_entry.get_text().strip() or _default_device_name()
            claim_btn.set_sensitive(False)
            _set_paste_status("Claiming the join request…")

            def worker() -> None:
                err: Exception | None = None
                try:
                    relay = _build_claimant_relay(config, join_url.relay_url)
                    claim_join_request(
                        relay, join_url.vault_id_undashed, join_url.join_request_id,
                        claimant_pubkey=claimant_pub, device_name=device_name,
                    )
                    log.info(
                        "vault.grant.claim_sent join_request_id=%s",
                        join_url.join_request_id[:18],
                    )
                except Exception as exc:  # noqa: BLE001
                    err = exc

                def settle() -> bool:
                    claim_btn.set_sensitive(True)
                    if err is not None:
                        _set_paste_status(_humanize_claim_error(err), "error")
                        return False
                    state["step"] = "verifying"
                    code_label.set_label(code)
                    stack.set_visible_child_name("verifying")
                    _start_polling()
                    return False

                GLib.idle_add(settle)

            threading.Thread(target=worker, daemon=True).start()

        claim_btn.connect("clicked", on_claim_clicked)

        # --- polling for approval -----------------------------------
        def _start_polling() -> None:
            if state["poll_source_id"] is not None:
                return
            state["poll_source_id"] = GLib.timeout_add_seconds(
                POLL_INTERVAL_S, _poll_tick,
            )

        def _stop_polling() -> None:
            if state["poll_source_id"] is not None:
                GLib.source_remove(state["poll_source_id"])
                state["poll_source_id"] = None

        def _poll_tick() -> bool:
            if state["cancelled"].is_set() or state["step"] != "verifying":
                state["poll_source_id"] = None
                return False
            _check_state_once()
            return True

        def _check_state_once() -> None:
            join_url: VaultJoinUrl = state["join_url"]

            def worker() -> None:
                fetched: JoinRequest | None = None
                err: Exception | None = None
                try:
                    relay = _build_claimant_relay(config, join_url.relay_url)
                    fetched = get_join_request(
                        relay, join_url.vault_id_undashed, join_url.join_request_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    err = exc

                def settle() -> bool:
                    if state["cancelled"].is_set():
                        return False
                    if err is not None:
                        if isinstance(err, JoinRequestNotFoundError):
                            _stop_polling()
                            _set_verify_status(
                                "The admin device rejected or deleted the "
                                "request. Ask them to start a fresh grant.",
                                "error",
                            )
                            return False
                        # Transient — keep polling, just surface the
                        # error inline.
                        _set_verify_status(
                            f"Network error (will keep retrying): "
                            f"{_humanize_poll_error(err)}",
                            "error",
                        )
                        return False
                    assert fetched is not None
                    if fetched.state == "approved":
                        _stop_polling()
                        _finalize_approval(fetched, win, success_body, stack)
                        return False
                    if fetched.state == "rejected":
                        _stop_polling()
                        _set_verify_status(
                            "The admin device rejected this request. "
                            "Close the window and try again from a fresh URL.",
                            "error",
                        )
                        return False
                    if fetched.state == "expired":
                        _stop_polling()
                        _set_verify_status(
                            "The join request expired. Ask the admin to "
                            "generate a fresh URL (≤ 15 min lifetime).",
                            "error",
                        )
                        return False
                    return False

                GLib.idle_add(settle)

            threading.Thread(target=worker, daemon=True).start()

        def _finalize_approval(
            fetched: JoinRequest, parent_win, body_label, view_stack,
        ) -> None:
            join_url: VaultJoinUrl = state["join_url"]
            wrapped = fetched.wrapped_vault_grant
            if wrapped is None:
                _set_verify_status(
                    "Relay reported approval but did not return the "
                    "wrapped grant. Ask the admin to retry.",
                    "error",
                )
                return
            try:
                payload = unwrap_grant_for_claimant(
                    envelope=wrapped,
                    claimant_priv=state["claimant_priv"],
                    admin_pub=join_url.ephemeral_pubkey,
                    expected_vault_id=join_url.vault_id_undashed,
                    expected_claimant_device_id=str(config.device_id or ""),
                )
            except GrantWrapError as exc:
                log.warning("vault.grant.unwrap_failed error=%s", exc)
                _set_verify_status(
                    "Could not decrypt the grant — possible tamper or "
                    f"role mismatch ({exc}). Close and try a fresh "
                    "request from the admin device.",
                    "error",
                )
                return

            try:
                _save_unlocked_grant(config_dir, config, payload)
                log.info(
                    "vault.grant.unwrap_succeeded vault=%s role=%s",
                    join_url.vault_id_dashed[:14], payload.approved_role,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("vault.grant.save_failed")
                _set_verify_status(
                    f"Could not save the unlocked vault locally: {exc}",
                    "error",
                )
                return

            body_label.set_label(
                f"You can now sync files in vault {join_url.vault_id_dashed} "
                f"with role {payload.approved_role!r}. Open the tray "
                "menu → Vault → Open Vault… to browse."
            )
            view_stack.set_visible_child_name("success")
            state["step"] = "success"

        def on_close(_win) -> bool:
            state["cancelled"].set()
            _stop_polling()
            # Best-effort secret scrub.
            priv = state.get("claimant_priv")
            if isinstance(priv, (bytes, bytearray)):
                try:
                    ba = bytearray(priv)
                    for i in range(len(ba)):
                        ba[i] = 0
                except Exception:  # noqa: BLE001
                    pass
            state["claimant_priv"] = None
            state["shared_secret"] = None
            return False

        win.connect("close-request", on_close)
        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ----- helpers -------------------------------------------------------


def _default_device_name() -> str:
    """Best-effort device label for the admin's approval dialog."""
    import socket

    try:
        return socket.gethostname() or "Linux desktop"
    except Exception:  # noqa: BLE001
        return "Linux desktop"


def _build_claimant_relay(config, relay_url_from_join):
    """Construct a :class:`VaultHttpRelay` pointed at the join URL's host.

    Claimant device may not have ``server_url`` configured yet (fresh
    install), so we temporarily steer the config at the URL embedded
    in the QR. We do NOT persist this — only the post-claim grant
    save updates the config's ``last_known_id``.
    """
    from .vault.binding.runtime import VaultHttpRelay

    config.reload()
    if not getattr(config, "server_url", None):
        # Steer the config in-memory at the relay host the QR carries.
        # The claim endpoint is unauthenticated against the vault (the
        # join_request_id is the bearer authority), but the device
        # still needs to be registered with the relay so device auth
        # works. Fresh installs hit a registration flow before reaching
        # the join wizard; if config has no server_url at this point
        # that's an operator error and the HTTP call will fail
        # explicitly.
        config.server_url = relay_url_from_join
    return VaultHttpRelay(config)


def _save_unlocked_grant(config_dir: Path, config, payload) -> None:
    """Persist the unwrapped vault material to the device's grant store
    and record ``last_known_id`` in the config so the tray submenu
    flips into the "operating" entries.
    """
    from .vault.binding.runtime import _vault_device_seed_provider
    from .vault.grant.store import VaultGrant, open_default_grant_store

    master_key = base64.b64decode(payload.vault_master_key_b64)
    if len(master_key) != 32:
        raise RuntimeError("vault_master_key did not decode to 32 bytes")
    grant = VaultGrant.from_bytes(
        payload.vault_id, master_key, payload.vault_access_secret,
    )
    try:
        store = open_default_grant_store(
            config_dir=Path(config_dir),
            device_seed_provider=_vault_device_seed_provider(Path(config_dir), config),
        )
        store.save(grant)
    finally:
        grant.zero()

    if "vault" not in config._data or not isinstance(config._data.get("vault"), dict):
        config._data["vault"] = {}
    config._data["vault"]["last_known_id"] = payload.vault_id
    config.save()


def _humanize_claim_error(exc: Exception) -> str:
    from .vault.grant.join_client import (
        JoinRequestAuthError,
        JoinRequestNotFoundError,
        JoinRequestRateLimitedError,
        JoinRequestStateError,
    )

    if isinstance(exc, JoinRequestNotFoundError):
        return "The join URL points to a request that no longer exists."
    if isinstance(exc, JoinRequestStateError):
        return (
            "Another device already claimed this join URL. Ask the admin "
            "to generate a fresh one."
        )
    if isinstance(exc, JoinRequestRateLimitedError):
        return (
            "The relay reports too many pending join-requests for this "
            "vault. Ask the admin to wait a few minutes and try again."
        )
    if isinstance(exc, JoinRequestAuthError):
        return (
            "This device isn't registered with the relay yet — finish "
            "Desktop Connector setup before joining a vault."
        )
    return f"Could not claim: {exc}"


def _humanize_poll_error(exc: Exception) -> str:
    return str(exc)


__all__ = ["show_vault_join"]
