"""Migration wizard subprocess (§5.C1).

GTK4 wizard that drives :func:`vault.migration.runner.run_migration`
end to end. Four linear pages — setup, confirm, progress, done — mirror
the import wizard's shape (``windows_vault_import.py``).

The engine handles all state transitions; this module is the GTK glue
+ worker-thread plumbing. Cancel/close at any stage is non-destructive
beyond what the engine already commits: if the operator backs out
mid-copy, the migration record stays at ``copying`` and a future
wizard reopen resumes from there. The state machine + crash recovery
live in ``vault.migration.state``.

The §5.M6 fix (`clear_previous_relay`) is invoked at the start of a
fresh migration so A → B → C records ``previous=B`` rather than the
stale A. §5.M2 landed 2026-05-18: the server now accepts genesis-
insert at any revision and skips the envelope author-match check
for ``expected=0`` so multi-device + edited-shard vaults migrate
cleanly. ``MigrationInventory.has_edited_shards`` stays as
diagnostic data but the wizard no longer warns on it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .vault.binding.runtime import (
    VaultHttpRelay,
    create_vault_relay,
    open_local_vault_from_grant,
)
from .vault.error_messages import humanize
from .vault.migration.propagation import can_switch_back
from .vault.migration.runner import (
    MigrationInventory,
    MigrationProgress,
    MigrationRunResult,
    migration_preflight,
    run_migration,
)
from .vault.migration.state import (
    MigrationRecord,
    clear_previous_relay,
    load_state,
    save_state,
)
from .windows_common import _make_app


log = logging.getLogger(__name__)


def show_vault_migration(config_dir: Path) -> None:
    """Top-level entry point for the ``vault-migration`` subprocess."""
    from .config import Config

    config = Config(config_dir)
    app = _make_app()

    state: dict = {
        "step": "setup",
        "vault_id": None,
        "source_url": None,
        "target_url": None,
        "inventory": None,           # MigrationInventory | None
        "result": None,              # MigrationRunResult | None
        "cancel_event": threading.Event(),
    }

    def on_activate(_app):
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)

        win = Adw.ApplicationWindow(
            application=app,
            title="Migrate vault to another relay",
            default_width=640,
            default_height=520,
        )
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        win.set_content(toolbar)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=14,
            margin_top=20, margin_bottom=20, margin_start=24, margin_end=24,
        )
        toolbar.set_content(outer)

        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        stack.set_hexpand(True)
        stack.set_vexpand(True)
        outer.append(stack)

        # ---- helpers ----------------------------------------------
        def go_to(name: str) -> None:
            state["step"] = name
            stack.set_visible_child_name(name)

        # ===== Page 1: Setup ======================================
        setup_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        setup_page.append(Gtk.Label(
            label="Migrate to another relay", xalign=0,
            css_classes=["title-2"],
        ))
        setup_page.append(Gtk.Label(
            label=(
                "Move every chunk + manifest revision of this vault to "
                "a new relay. The source relay's row stays sealed and "
                "read-only after commit. Within 7 days you can switch "
                "back to the source from Vault Settings → Migration."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        source_label = Gtk.Label(xalign=0, css_classes=["monospace"])
        setup_page.append(_row("Source relay (read-only)", source_label))

        target_entry = Gtk.Entry(
            placeholder_text="https://new-relay.example.com",
        )
        target_entry.set_hexpand(True)
        setup_page.append(_row("Target relay URL", target_entry))

        setup_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        setup_page.append(setup_status)

        setup_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        setup_cancel = Gtk.Button(label="Cancel", css_classes=["pill"])
        setup_cancel.connect("clicked", lambda _b: win.close())
        setup_actions.append(setup_cancel)
        test_btn = Gtk.Button(label="Test connection", css_classes=["pill"])
        setup_actions.append(test_btn)
        preflight_btn = Gtk.Button(
            label="Preflight migration", css_classes=["pill", "suggested-action"],
        )
        setup_actions.append(preflight_btn)
        setup_page.append(setup_actions)
        stack.add_named(setup_page, "setup")

        # ===== Page 2: Confirm ====================================
        confirm_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        confirm_page.append(Gtk.Label(
            label="Confirm migration", xalign=0, css_classes=["title-2"],
        ))
        confirm_summary = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
        )
        confirm_page.append(confirm_summary)
        confirm_warning = Gtk.Label(
            xalign=0, wrap=True, css_classes=["error"],
        )
        confirm_warning.set_visible(False)
        confirm_page.append(confirm_warning)
        confirm_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        confirm_back = Gtk.Button(label="Back", css_classes=["pill"])
        confirm_back.connect("clicked", lambda _b: go_to("setup"))
        confirm_actions.append(confirm_back)
        start_btn = Gtk.Button(
            label="Start migration",
            css_classes=["pill", "destructive-action"],
        )
        confirm_actions.append(start_btn)
        confirm_page.append(confirm_actions)
        stack.add_named(confirm_page, "confirm")

        # ===== Page 3: Progress ===================================
        progress_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        progress_page.append(Gtk.Label(
            label="Migration in progress",
            xalign=0, css_classes=["title-2"],
        ))
        progress_phase_label = Gtk.Label(
            label="Preparing target relay…", xalign=0,
            css_classes=["heading"],
        )
        progress_page.append(progress_phase_label)
        progress_bar = Gtk.ProgressBar()
        progress_bar.set_show_text(True)
        progress_page.append(progress_bar)
        progress_status = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
        )
        progress_page.append(progress_status)
        progress_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        progress_cancel = Gtk.Button(label="Cancel", css_classes=["pill"])
        progress_actions.append(progress_cancel)
        progress_page.append(progress_actions)
        stack.add_named(progress_page, "progress")

        # ===== Page 4: Done =======================================
        done_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        done_page.append(Gtk.Label(
            label="Migration complete",
            xalign=0, css_classes=["title-2"],
        ))
        done_status = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
        )
        done_page.append(done_status)
        done_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        done_close = Gtk.Button(
            label="Close", css_classes=["pill", "suggested-action"],
        )
        done_close.connect("clicked", lambda _b: win.close())
        done_actions.append(done_close)
        done_page.append(done_actions)
        stack.add_named(done_page, "done")

        # ===== Page 5: Error / verify mismatch ====================
        error_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        error_page.append(Gtk.Label(
            label="Migration could not complete",
            xalign=0, css_classes=["title-2"],
        ))
        error_status = Gtk.Label(
            xalign=0, wrap=True, css_classes=["error"],
        )
        error_page.append(error_status)
        error_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.END,
        )
        error_close = Gtk.Button(label="Close", css_classes=["pill"])
        error_close.connect("clicked", lambda _b: win.close())
        error_actions.append(error_close)
        error_page.append(error_actions)
        stack.add_named(error_page, "error")

        # --- initial population ------------------------------------
        vault_id_undashed = _resolve_active_vault_id(config)
        state["vault_id"] = vault_id_undashed
        source_url = str(getattr(config, "server_url", "") or "")
        state["source_url"] = source_url
        source_label.set_label(source_url or "(not set)")

        # If a previous migration is mid-stream, surface a resume hint.
        existing = load_state(config_dir)
        if existing is not None and existing.vault_id == vault_id_undashed:
            setup_status.add_css_class("error")
            setup_status.remove_css_class("dim-label")
            setup_status.set_label(
                f"A previous migration to {existing.target_relay_url!r} "
                f"is mid-stream (state: {existing.state}). Entering the "
                "same URL resumes it."
            )
            target_entry.set_text(existing.target_relay_url)

        # --- setup handlers ----------------------------------------
        def _set_setup_status(text: str, kind: str = "neutral") -> None:
            setup_status.set_label(text)
            setup_status.remove_css_class("error")
            setup_status.remove_css_class("success")
            setup_status.remove_css_class("dim-label")
            if kind == "error":
                setup_status.add_css_class("error")
            elif kind == "success":
                setup_status.add_css_class("success")
            else:
                setup_status.add_css_class("dim-label")

        def _validate_target_url() -> str | None:
            raw = target_entry.get_text().strip()
            if not raw:
                _set_setup_status("Target relay URL is required.", "error")
                return None
            parsed = urlparse(raw)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                _set_setup_status(
                    "Target URL must include scheme + host (http(s)://host).",
                    "error",
                )
                return None
            if raw.rstrip("/") == source_url.rstrip("/"):
                _set_setup_status(
                    "Target URL must differ from the current source relay.",
                    "error",
                )
                return None
            return raw.rstrip("/")

        def on_test_connection(_btn) -> None:
            target_url = _validate_target_url()
            if not target_url:
                return
            _set_setup_status("Probing target relay…")
            test_btn.set_sensitive(False)
            preflight_btn.set_sensitive(False)

            def worker() -> None:
                err: Exception | None = None
                try:
                    _probe_relay_health(target_url)
                except Exception as exc:  # noqa: BLE001
                    err = exc

                def settle() -> bool:
                    test_btn.set_sensitive(True)
                    preflight_btn.set_sensitive(True)
                    if err is not None:
                        _set_setup_status(
                            f"Could not reach the target relay: "
                            f"{humanize(err)}",
                            "error",
                        )
                        return False
                    _set_setup_status(
                        f"Target relay reachable at {target_url}. "
                        "Click Preflight migration to continue.",
                        "success",
                    )
                    return False

                GLib.idle_add(settle)

            threading.Thread(target=worker, daemon=True).start()

        test_btn.connect("clicked", on_test_connection)

        def on_preflight(_btn) -> None:
            target_url = _validate_target_url()
            if not target_url:
                return
            state["target_url"] = target_url
            _set_setup_status("Inspecting source vault inventory…")
            test_btn.set_sensitive(False)
            preflight_btn.set_sensitive(False)

            def worker() -> None:
                err: Exception | None = None
                inventory: MigrationInventory | None = None
                try:
                    config.reload()
                    source_relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(
                        config_dir, config, vault_id_undashed,
                    )
                    try:
                        inventory = migration_preflight(
                            vault=vault, source_relay=source_relay,
                        )
                    finally:
                        vault.close()
                except Exception as exc:  # noqa: BLE001
                    err = exc

                def settle() -> bool:
                    test_btn.set_sensitive(True)
                    preflight_btn.set_sensitive(True)
                    if err is not None:
                        _set_setup_status(
                            f"Could not inspect source vault: "
                            f"{humanize(err)}",
                            "error",
                        )
                        return False
                    state["inventory"] = inventory
                    _render_confirm_page(inventory, target_url)
                    go_to("confirm")
                    return False

                GLib.idle_add(settle)

            threading.Thread(target=worker, daemon=True).start()

        preflight_btn.connect("clicked", on_preflight)

        # --- confirm handlers --------------------------------------
        def _render_confirm_page(inventory: MigrationInventory, target_url: str) -> None:
            mb = inventory.ciphertext_bytes_total / (1024 * 1024)
            confirm_summary.set_label(
                f"About to migrate vault {_dashed(vault_id_undashed)} "
                f"from {source_url} to {target_url}.\n\n"
                f"• Chunks to copy: {inventory.chunk_count}\n"
                f"• Folder count: {inventory.remote_folder_count}\n"
                f"• Cumulative ciphertext: {mb:.2f} MiB"
            )
            # §5.M2 landed: edited-shard migrations are no longer
            # gated. ``has_edited_shards`` stays on the inventory as
            # diagnostic data; the wizard no longer surfaces a
            # warning since the server now accepts genesis-insert
            # for any revision and skips the envelope author-match
            # check for ``expected=0``.
            confirm_warning.set_visible(False)

        def on_start_migration(_btn) -> None:
            target_url = state["target_url"]
            if not target_url:
                go_to("setup")
                return
            start_btn.set_sensitive(False)
            confirm_back.set_sensitive(False)

            # §5.M6: clear any stale previous_relay_url from an earlier
            # migration so the new commit stamps `previous = current source`.
            existing_state = load_state(config_dir)
            if existing_state is not None:
                cleaned = clear_previous_relay(existing_state)
                if cleaned != existing_state:
                    save_state(cleaned, config_dir)

            state["cancel_event"] = threading.Event()
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("0 / ? chunks")
            progress_phase_label.set_label("Preparing target relay…")
            progress_status.set_label("")
            go_to("progress")
            _kick_run(target_url)

        start_btn.connect("clicked", on_start_migration)

        # --- progress + run ----------------------------------------
        def _on_progress(p: MigrationProgress) -> None:
            def update() -> bool:
                progress_phase_label.set_label(_phase_label(p.phase))
                if p.chunks_total > 0:
                    progress_bar.set_fraction(
                        min(1.0, p.chunks_copied / max(1, p.chunks_total)),
                    )
                    progress_bar.set_text(
                        f"{p.chunks_copied} / {p.chunks_total} chunks"
                        + (
                            f" ({p.chunks_skipped} skipped)"
                            if p.chunks_skipped else ""
                        )
                    )
                mb = p.bytes_copied / (1024 * 1024)
                progress_status.set_label(f"{mb:.2f} MiB copied so far.")
                return False

            GLib.idle_add(update)

        def _kick_run(target_url: str) -> None:
            def worker() -> None:
                result: MigrationRunResult | None = None
                err: Exception | None = None
                try:
                    config.reload()
                    source_relay = create_vault_relay(config)
                    target_relay = VaultHttpRelay.__new__(VaultHttpRelay)
                    # Build target relay bound to the new URL but reusing
                    # this device's auth (the relay still authenticates
                    # the device via the registration token; vault auth
                    # comes from the vault_access_secret separately).
                    target_relay._config = config  # type: ignore[attr-defined]
                    from .connection import ConnectionManager
                    target_relay._conn = ConnectionManager(  # type: ignore[attr-defined]
                        target_url, config.device_id, config.auth_token,
                    )
                    vault = open_local_vault_from_grant(
                        config_dir, config, vault_id_undashed,
                    )
                    try:
                        result = run_migration(
                            vault=vault,
                            source_relay=source_relay,
                            target_relay=target_relay,
                            source_relay_url=state["source_url"],
                            target_relay_url=target_url,
                            config_dir=config_dir,
                            progress=_on_progress,
                            on_committed=lambda rec: _commit_callback(rec, target_url),
                            # F-510 Phase 3.1: needed by the post-commit
                            # audit publish on the target relay.
                            author_device_id=str(
                                getattr(config, "device_id", "") or ""
                            ),
                        )
                    finally:
                        vault.close()
                except Exception as exc:  # noqa: BLE001
                    err = exc

                def settle() -> bool:
                    if err is not None:
                        error_status.set_label(
                            f"Migration aborted: {humanize(err)}\n\n"
                            "The migration record is preserved; reopen "
                            "this wizard to resume from the same state."
                        )
                        go_to("error")
                        return False
                    state["result"] = result
                    assert result is not None
                    if not result.verify.matches:
                        error_status.set_label(
                            "Verify failed — the target relay's hash "
                            "chain disagreed with the source. Mismatches: "
                            f"{', '.join(result.verify.mismatches)}. "
                            "The migration has NOT committed."
                        )
                        go_to("error")
                        return False
                    _render_done_page(result, target_url)
                    go_to("done")
                    return False

                GLib.idle_add(settle)

            threading.Thread(target=worker, daemon=True).start()

        def _commit_callback(record: MigrationRecord, target_url: str) -> None:
            """Persist the post-commit relay flip into config.json.

            Runs inside :func:`run_migration`'s ``on_committed`` gate,
            so a crash here keeps the state file at ``committed`` and
            the next wizard launch retries (F-C15).
            """
            log.info(
                "vault.migration.commit_callback vault=%s target=%s",
                record.vault_id, target_url,
            )
            config.reload()
            config.vault_previous_relay_url = state["source_url"]
            config.vault_previous_relay_expires_at = record.committed_at
            config.server_url = target_url
            config.save()

        def on_progress_cancel(_btn) -> None:
            state["cancel_event"].set()
            progress_status.set_label(
                "Cancel requested. The migration record is preserved; "
                "you can resume by reopening this wizard."
            )

        progress_cancel.connect("clicked", on_progress_cancel)

        # --- done --------------------------------------------------
        def _render_done_page(result: MigrationRunResult, target_url: str) -> None:
            mb = result.bytes_copied / (1024 * 1024)
            available = can_switch_back(
                previous_relay_url=config.vault_previous_relay_url,
                previous_relay_expires_at=config.vault_previous_relay_expires_at,
            )
            done_status.set_label(
                f"Vault migrated to {target_url}.\n\n"
                f"• Chunks copied: {result.chunks_copied} "
                f"({result.chunks_skipped} skipped)\n"
                f"• Ciphertext copied: {mb:.2f} MiB\n"
                f"• Verify: matched ({result.verify.sample_passed}"
                f"/{result.verify.sample_size} samples)\n\n"
                + (
                    "Switch-back available from Vault Settings → "
                    "Migration for the next 7 days."
                    if available else
                    "Switch-back grace window already elapsed."
                )
            )

        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


# ---- helpers --------------------------------------------------------


def _row(label_text: str, value_widget: Gtk.Widget) -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    lbl = Gtk.Label(label=label_text, xalign=0, css_classes=["dim-label"])
    row.append(lbl)
    row.append(value_widget)
    return row


def _phase_label(phase: str) -> str:
    return {
        "preparing": "Preparing target relay…",
        "copying": "Copying chunks…",
        "verifying": "Verifying hash chain on target…",
        "committing": "Committing on source relay…",
    }.get(phase, phase.replace("_", " ").capitalize())


def _resolve_active_vault_id(config) -> str:
    config.reload()
    raw = config._data.get("vault")
    if isinstance(raw, dict):
        vid = raw.get("last_known_id")
        if isinstance(vid, str):
            return vid
    return ""


def _dashed(vault_id_undashed: str) -> str:
    v = vault_id_undashed
    if len(v) == 12:
        return f"{v[:4]}-{v[4:8]}-{v[8:12]}"
    return v or "(no vault)"


def _probe_relay_health(target_url: str) -> None:
    """Hit the target relay's health endpoint to verify reachability.

    A successful HTTP response — any status, even 4xx — indicates the
    relay is up. The migration's vault_access_token validation happens
    later, when ``migration_start`` POSTs to the source.
    """
    import urllib.request

    req = urllib.request.Request(
        f"{target_url.rstrip('/')}/api/health",
        headers={"User-Agent": "desktop-connector-migration-preflight"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return
    except urllib.error.HTTPError:
        # 4xx is still "reachable"; the migration code will surface
        # auth failures later when it actually posts.
        return


__all__ = ["show_vault_migration"]
