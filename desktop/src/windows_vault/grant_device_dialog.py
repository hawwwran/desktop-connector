"""Admin-side "Grant a new device" wizard dialog (§5.C2).

Spawned from the Devices tab's "Grant a new device" button. Walks the
admin through the QR-grant flow:

1. **Generate**: mint an ephemeral X25519 keypair, POST
   ``createJoinRequest`` so the relay allocates a ``jr_v1_…`` id.
2. **Share**: render the ``vault://…`` join URL as a QR code + plain
   text. Poll ``getJoinRequest`` every 2 s until ``state="claimed"``.
3. **Verify + approve**: show the 6-digit verification code derived
   from the X25519 shared secret with the claimant's pubkey. Operator
   picks a role (read-only / browse-upload / sync / admin), reads the
   code aloud to the claimant, and clicks Approve. The dialog wraps a
   :class:`GrantPayload` for the claimant's pubkey and posts
   ``approveJoinRequest``.

Cancel / window close at any pre-approve stage issues a best-effort
DELETE to the relay so abandoned rows don't sit in the per-vault
pending budget (cap is 5; spec §10).
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402


log = logging.getLogger(__name__)


POLL_INTERVAL_S = 2

ROLE_CHOICES: list[tuple[str, str]] = [
    ("sync", "Sync — read, write, run autosync (recommended for new desktops)"),
    ("browse-upload", "Browse + upload — read, upload, soft-delete"),
    ("read-only", "Read-only — view files + history"),
    ("admin", "Admin — full access including grants + purges"),
]


def build_grant_device_dialog(
    parent_window,
    *,
    config,
    config_dir,
    vault_id_undashed: str,
    vault_id_dashed: str,
    on_grant_landed: Callable[[], None] | None = None,
) -> None:
    """Open the wizard dialog. Returns immediately; the dialog runs its
    own event lifecycle on the GTK main loop.

    ``on_grant_landed`` fires after a successful approve so the caller
    (Devices tab) can refresh its grant list.
    """
    from ..vault.grant.qr import (
        DEFAULT_TTL_SECONDS,
        derive_shared_secret,
        derive_verification_code,
        make_join_url,
    )

    state: dict[str, Any] = {
        "step": "generating",
        "admin_priv": None,
        "admin_pub": None,
        "join_request_id": None,
        "join_url": None,
        "claimant_pubkey": None,
        "claimant_device_id": None,
        "device_name": None,
        "verification_code": None,
        "poll_source_id": None,
        "cancelled": threading.Event(),
        "approved": False,
    }

    dlg = Adw.Dialog()
    dlg.set_title("Grant a new device")
    dlg.set_content_width(560)
    dlg.set_content_height(520)

    body = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=14,
        margin_top=20, margin_bottom=20, margin_start=20, margin_end=20,
    )
    dlg.set_child(body)

    body.append(Gtk.Label(
        label="Grant a new device",
        xalign=0, css_classes=["title-2"],
    ))

    stack = Gtk.Stack()
    stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
    stack.set_hexpand(True)
    stack.set_vexpand(True)
    body.append(stack)

    # ----- generating page -------------------------------------------
    gen_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    gen_status = Gtk.Label(
        label="Preparing a one-time join URL…",
        xalign=0, wrap=True, css_classes=["dim-label"],
    )
    gen_page.append(gen_status)
    gen_spinner = Gtk.Spinner()
    gen_spinner.set_size_request(48, 48)
    gen_spinner.set_halign(Gtk.Align.START)
    gen_spinner.start()
    gen_page.append(gen_spinner)
    stack.add_named(gen_page, "generating")

    # ----- share page ------------------------------------------------
    # Wrap in a ScrolledWindow so a Settings window narrower than the
    # dialog's preferred 560×520 stays usable: the QR + URL entry +
    # hint can scroll even when the dialog gets clipped by an
    # unusually small parent. Adw.Dialog adapts to its parent, so
    # without this the share-step contents could fall below the
    # action row.
    share_page_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    share_page_inner.append(Gtk.Label(
        label=(
            "On the new device, open the tray menu → Vault → "
            "Add this device to a vault…, then paste the URL below "
            "(or scan the QR)."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))
    qr_image = Gtk.Picture()
    # The QR's natural size depends on the URL length (~200 px wide with
    # the box_size=5 / border=2 settings in ``_qr_to_paintable``); let it
    # shrink to fit narrow parent windows but never scale UP past natural
    # — at 1× the modules are crisp; an enlarged copy would just blur.
    qr_image.set_can_shrink(True)
    qr_image.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
    qr_image.set_size_request(200, 200)
    qr_image.set_halign(Gtk.Align.CENTER)
    share_page_inner.append(qr_image)
    url_view = Gtk.Entry(editable=False)
    url_view.set_hexpand(True)
    url_view.add_css_class("monospace")
    share_page_inner.append(url_view)
    share_hint = Gtk.Label(
        label="Waiting for the new device to claim this URL…",
        xalign=0, wrap=True, css_classes=["dim-label"],
    )
    share_page_inner.append(share_hint)
    share_page = Gtk.ScrolledWindow()
    share_page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    share_page.set_child(share_page_inner)
    share_page.set_vexpand(True)
    stack.add_named(share_page, "share")

    # ----- verify page -----------------------------------------------
    # Same scroll safety net as the share page — the role dropdown +
    # status line below the code label can otherwise fall under the
    # action row on a tight Settings window.
    verify_page_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    verify_page_inner.append(Gtk.Label(
        label=(
            "Compare this 6-digit code with the new device's screen. "
            "If they match exactly, the link is secure — proceed with "
            "Approve. If they differ, cancel and try again."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))
    code_label = Gtk.Label(
        label="•••-•••", xalign=0.5,
        css_classes=["title-1", "monospace"],
    )
    code_label.set_hexpand(True)
    verify_page_inner.append(code_label)

    claimant_meta = Gtk.Label(
        label="", xalign=0, wrap=True, css_classes=["dim-label"],
    )
    verify_page_inner.append(claimant_meta)

    role_label = Gtk.Label(
        label="Role to grant:", xalign=0, css_classes=["heading"],
    )
    verify_page_inner.append(role_label)
    role_combo = Gtk.DropDown.new_from_strings([label for _r, label in ROLE_CHOICES])
    role_combo.set_halign(Gtk.Align.START)
    verify_page_inner.append(role_combo)
    verify_status = Gtk.Label(
        label="", xalign=0, wrap=True, css_classes=["dim-label"],
    )
    verify_page_inner.append(verify_status)
    verify_page = Gtk.ScrolledWindow()
    verify_page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    verify_page.set_child(verify_page_inner)
    verify_page.set_vexpand(True)
    stack.add_named(verify_page, "verify")

    # ----- done page -------------------------------------------------
    done_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    done_status = Gtk.Label(
        xalign=0, wrap=True, css_classes=["dim-label"],
    )
    done_page.append(done_status)
    stack.add_named(done_page, "done")

    # ----- error page ------------------------------------------------
    err_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    err_status = Gtk.Label(
        xalign=0, wrap=True, css_classes=["error"],
    )
    err_page.append(err_status)
    stack.add_named(err_page, "error")

    # ----- action row ------------------------------------------------
    actions = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        halign=Gtk.Align.END,
    )
    body.append(actions)

    close_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
    actions.append(close_btn)

    reject_btn = Gtk.Button(
        label="Reject", css_classes=["pill", "destructive-action"],
    )
    reject_btn.set_visible(False)
    actions.append(reject_btn)

    approve_btn = Gtk.Button(
        label="Approve", css_classes=["pill", "suggested-action"],
    )
    approve_btn.set_visible(False)
    actions.append(approve_btn)

    # --- helpers -----------------------------------------------------
    def _set_verify_status(text: str, kind: str = "neutral") -> None:
        verify_status.set_label(text)
        verify_status.remove_css_class("error")
        verify_status.remove_css_class("success")
        if kind == "error":
            verify_status.add_css_class("error")
        elif kind == "success":
            verify_status.add_css_class("success")

    def _show_error(message: str) -> None:
        log.warning("vault.grant.dialog_error message=%s", message)
        err_status.set_label(message)
        stack.set_visible_child_name("error")
        approve_btn.set_visible(False)
        reject_btn.set_visible(False)

    def _stop_polling() -> None:
        if state["poll_source_id"] is not None:
            GLib.source_remove(state["poll_source_id"])
            state["poll_source_id"] = None

    def _start_polling() -> None:
        if state["poll_source_id"] is None:
            state["poll_source_id"] = GLib.timeout_add_seconds(
                POLL_INTERVAL_S, _poll_tick,
            )

    # --- step 1: generate the join-request ---------------------------
    def _begin_generate() -> None:
        admin_priv, admin_pub = _generate_x25519_keypair()
        state["admin_priv"] = admin_priv
        state["admin_pub"] = admin_pub

        def worker() -> None:
            err: Exception | None = None
            join_request = None
            url = None
            try:
                from ..vault.binding.runtime import (
                    create_vault_relay, open_local_vault_from_grant,
                )
                from ..vault.grant.join_client import create_join_request

                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    join_request = create_join_request(
                        relay, vault.vault_id, vault.vault_access_secret,
                        ephemeral_admin_pubkey=admin_pub,
                    )
                    url = make_join_url(
                        relay_url=config.server_url,
                        vault_id=vault_id_undashed,
                        join_request_id=join_request.join_request_id,
                        ephemeral_pubkey=admin_pub,
                        ttl_seconds=DEFAULT_TTL_SECONDS,
                    )
                    log.info(
                        "vault.grant.join_request_created jr=%s",
                        join_request.join_request_id[:18],
                    )
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                err = exc

            def settle() -> bool:
                if err is not None:
                    _show_error(f"Could not start the grant: {err}")
                    return False
                state["join_request_id"] = join_request.join_request_id
                state["join_url"] = url
                _render_share_step(url)
                stack.set_visible_child_name("share")
                state["step"] = "share"
                _start_polling()
                return False

            GLib.idle_add(settle)

        threading.Thread(target=worker, daemon=True).start()

    def _render_share_step(url: str) -> None:
        url_view.set_text(url)
        try:
            qr_paintable = _qr_to_paintable(url)
            qr_image.set_paintable(qr_paintable)
        except Exception:  # noqa: BLE001
            # PIL or qrcode missing on the host — degrade to URL-only.
            log.exception("vault.grant.qr_render_failed")

    # --- step 2: poll for claim --------------------------------------
    def _poll_tick() -> bool:
        if state["cancelled"].is_set() or state["step"] not in ("share", "verify"):
            state["poll_source_id"] = None
            return False
        _poll_once()
        return True

    def _poll_once() -> None:
        join_request_id = state["join_request_id"]
        if not join_request_id:
            return

        def worker() -> None:
            from ..vault.binding.runtime import (
                create_vault_relay, open_local_vault_from_grant,
            )
            from ..vault.grant.join_client import get_join_request

            fetched = None
            err: Exception | None = None
            try:
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    fetched = get_join_request(
                        relay, vault.vault_id, join_request_id,
                        vault_access_secret=vault.vault_access_secret,
                    )
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                err = exc

            def settle() -> bool:
                if state["cancelled"].is_set():
                    return False
                if err is not None:
                    # Transient — log + keep polling.
                    log.warning("vault.grant.poll_error error=%s", err)
                    return False
                if fetched.state == "claimed" and state["step"] == "share":
                    _on_claim_landed(fetched)
                elif fetched.state == "expired":
                    _stop_polling()
                    _show_error(
                        "The join URL expired before the new device claimed it. "
                        "Close the dialog and try again."
                    )
                elif fetched.state == "rejected":
                    _stop_polling()
                    _show_error("The join request was rejected.")
                return False

            GLib.idle_add(settle)

        threading.Thread(target=worker, daemon=True).start()

    def _on_claim_landed(fetched) -> None:
        claimant_pub = fetched.claimant_pubkey
        if claimant_pub is None:
            _show_error("Claim arrived but the relay did not include the claimant pubkey.")
            return
        admin_priv = state["admin_priv"]
        shared = derive_shared_secret(bytes(admin_priv), claimant_pub)
        code = derive_verification_code(shared)

        state["claimant_pubkey"] = claimant_pub
        state["claimant_device_id"] = fetched.claimant_device_id
        state["device_name"] = fetched.device_name or ""
        state["verification_code"] = code

        code_label.set_label(code)
        claimant_meta.set_label(
            f"New device: {fetched.device_name or '(unnamed)'} "
            f"({(fetched.claimant_device_id or '')[:12]}…)"
        )
        approve_btn.set_visible(True)
        reject_btn.set_visible(True)
        state["step"] = "verify"
        stack.set_visible_child_name("verify")

    # --- step 3: approve ---------------------------------------------
    def on_approve(_btn) -> None:
        from ..vault.binding.runtime import (
            create_vault_relay, open_local_vault_from_grant,
        )
        from ..vault.grant.join_client import approve_join_request
        from ..vault.grant.wrap import GrantPayload, wrap_grant_for_claimant

        idx = role_combo.get_selected()
        if idx < 0 or idx >= len(ROLE_CHOICES):
            _set_verify_status("Pick a role to grant.", "error")
            return
        role_token = ROLE_CHOICES[idx][0]
        claimant_pubkey = state["claimant_pubkey"]
        claimant_device_id = state["claimant_device_id"]
        if claimant_pubkey is None or not claimant_device_id:
            _set_verify_status("Internal state missing — close and retry.", "error")
            return

        approve_btn.set_sensitive(False)
        reject_btn.set_sensitive(False)
        _set_verify_status("Wrapping the grant and sending approval…")

        def worker() -> None:
            err: Exception | None = None
            try:
                config.reload()
                relay = create_vault_relay(config)
                vault = open_local_vault_from_grant(
                    config_dir, config, vault_id_undashed,
                )
                try:
                    grant_id = _generate_grant_id()
                    granted_at = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )
                    payload = GrantPayload(
                        vault_id=vault.vault_id,
                        grant_id=grant_id,
                        claimant_device_id=claimant_device_id,
                        approved_role=role_token,
                        granted_by_device_id=str(config.device_id or ""),
                        granted_at=granted_at,
                        vault_master_key_b64=base64.b64encode(
                            vault.master_key,
                        ).decode("ascii"),
                        vault_access_secret=vault.vault_access_secret,
                    )
                    envelope = wrap_grant_for_claimant(
                        payload=payload,
                        admin_priv=bytes(state["admin_priv"]),
                        claimant_pub=claimant_pubkey,
                    )
                    approve_join_request(
                        relay, vault.vault_id, vault.vault_access_secret,
                        state["join_request_id"],
                        approved_role=role_token,
                        wrapped_vault_grant=envelope,
                    )
                    log.info(
                        "vault.grant.approved role=%s claimant=%s",
                        role_token, claimant_device_id[:12],
                    )
                    # F-510 Phase 3.1 Wire 4: best-effort audit row on
                    # the encrypted manifest so every device's Activity
                    # tab sees this grant.
                    from ..vault.grant.audit import (
                        publish_grant_lifecycle_audit,
                    )
                    publish_grant_lifecycle_audit(
                        vault=vault, relay=relay,
                        event_type="vault.grant.created",
                        author_device_id=str(config.device_id or ""),
                        extra={
                            "approved_role": role_token,
                            "claimant_device_id": str(claimant_device_id),
                        },
                    )
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                err = exc

            def settle() -> bool:
                approve_btn.set_sensitive(True)
                reject_btn.set_sensitive(True)
                if err is not None:
                    _set_verify_status(f"Approval failed: {err}", "error")
                    return False
                state["approved"] = True
                state["step"] = "done"
                _stop_polling()
                done_status.set_label(
                    f"Granted {role_token!r} access to "
                    f"{state['device_name'] or 'the new device'}. "
                    "You can close this dialog."
                )
                stack.set_visible_child_name("done")
                approve_btn.set_visible(False)
                reject_btn.set_visible(False)
                close_btn.set_label("Close")
                if on_grant_landed is not None:
                    on_grant_landed()
                return False

            GLib.idle_add(settle)

        threading.Thread(target=worker, daemon=True).start()

    approve_btn.connect("clicked", on_approve)

    def on_reject(_btn) -> None:
        _reject_and_close()

    reject_btn.connect("clicked", on_reject)

    def _reject_and_close() -> None:
        join_request_id = state["join_request_id"]
        state["cancelled"].set()
        _stop_polling()
        if not state["approved"] and join_request_id:
            _delete_join_request_best_effort(
                config, config_dir, vault_id_undashed, join_request_id,
            )
        log.info("vault.grant.rejected jr=%s", (join_request_id or "")[:18])
        dlg.close()

    def on_close(_btn) -> None:
        if state["approved"]:
            dlg.close()
            return
        _reject_and_close()

    close_btn.connect("clicked", on_close)

    def on_dlg_close(_d) -> None:
        # Window-X / Escape route: same as Cancel for an unapproved
        # request, no-op for a finished one.
        state["cancelled"].set()
        _stop_polling()
        if not state["approved"]:
            jr = state.get("join_request_id")
            if jr:
                _delete_join_request_best_effort(
                    config, config_dir, vault_id_undashed, jr,
                )
        # C2/C3: zero the LIVE X25519 private scalar bytes. Workers
        # that captured ``bytes(admin_priv)`` snapshots for
        # derive_shared_secret / wrap_grant_for_claimant have
        # already returned by this point; the live bytearray is
        # the canonical storage we can actually scrub.
        admin_priv = state.get("admin_priv")
        if isinstance(admin_priv, bytearray):
            for i in range(len(admin_priv)):
                admin_priv[i] = 0
        state["admin_priv"] = None

    dlg.connect("closed", on_dlg_close)

    dlg.present(parent_window)
    _begin_generate()


# ----- module helpers -----------------------------------------------


def _generate_x25519_keypair() -> tuple[bytearray, bytes]:
    """Mint a fresh X25519 ephemeral keypair for the grant exchange.

    Private scalar is a ``bytearray`` (mutable) so the dialog's
    close handler can zero its live bytes — pre-fix this was
    ``bytes`` which is immutable, so the documented "zero on close"
    pattern wrote into a copy and the real allocation sat on the
    heap until GC.
    """
    from nacl.bindings import crypto_scalarmult_base
    priv = bytearray(secrets.token_bytes(32))
    pub = crypto_scalarmult_base(bytes(priv))
    return priv, pub


_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"


def _generate_grant_id() -> str:
    """Mint a ``gr_v1_<24base32>`` id matching the server's regex.

    Server also mints its own grant_id for the device-grants row;
    this id is for the AEAD envelope's deterministic prefix only
    (it pins the wrapped payload so it can't be replayed onto a
    different grant slot).
    """
    raw = secrets.token_bytes(15)
    out: list[str] = []
    buf = 0
    bits = 0
    for byte in raw:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_BASE32_LOWER[(buf >> bits) & 0x1f])
    return "gr_v1_" + "".join(out[:24])


def _qr_to_paintable(url: str):
    """Render ``url`` as a Gdk.Paintable suitable for ``Gtk.Picture``.

    Uses the bundled ``qrcode`` library (already a Desktop Connector
    dependency for the existing pairing flow). The PIL image is
    serialised to PNG bytes then decoded into a Gdk.Texture via
    GdkPixbuf — same path used elsewhere when the desktop needs to
    show an in-memory image without writing to disk.
    """
    import io

    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        # box_size=5 + border=2 keeps the natural QR ~200 px wide for a
        # typical join URL, comfortable inside the 560×520 dialog and
        # easily scannable on a phone camera at 1:1 scale. The previous
        # box_size=8 / border=4 produced ~420 px QRs that overflowed
        # the dialog's content height and got clipped by the parent
        # window (Vault Settings).
        box_size=5, border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    png_bytes = io.BytesIO()
    img.save(png_bytes, format="PNG")
    png_bytes.seek(0)
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(png_bytes.getvalue())
    loader.close()
    pixbuf = loader.get_pixbuf()
    return Gdk.Texture.new_for_pixbuf(pixbuf)


def _delete_join_request_best_effort(
    config, config_dir, vault_id_undashed: str, join_request_id: str,
) -> None:
    """Fire DELETE on a worker thread; swallow errors.

    Abandoned join-requests sit in the per-vault budget (cap 5) until
    their 15-min TTL drains, so we try to clean them up explicitly
    when the operator cancels.
    """
    from ..vault.binding.runtime import (
        create_vault_relay, open_local_vault_from_grant,
    )
    from ..vault.grant.join_client import reject_join_request

    def worker() -> None:
        try:
            config.reload()
            relay = create_vault_relay(config)
            vault = open_local_vault_from_grant(
                config_dir, config, vault_id_undashed,
            )
            try:
                reject_join_request(
                    relay, vault.vault_id, vault.vault_access_secret,
                    join_request_id,
                )
                log.info(
                    "vault.grant.rejected jr=%s via=cancel",
                    join_request_id[:18],
                )
            finally:
                vault.close()
        except Exception:  # noqa: BLE001
            log.exception("vault.grant.delete_failed jr=%s", join_request_id[:18])

    threading.Thread(target=worker, daemon=True).start()


__all__ = ["build_grant_device_dialog"]
