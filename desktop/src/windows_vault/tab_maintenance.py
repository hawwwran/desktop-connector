"""Maintenance tab — debug bundle + integrity check (F-501).

Extracted from ``windows_vault.py`` (lines ~726–986).
"""

import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

from ..vault.error_messages import humanize
from ._main_context import MainContext


def build_maintenance_tab(ctx: MainContext, win) -> "Gtk.Box":
    config = ctx.config
    config_dir = ctx.config_dir
    vault_id_undashed = ctx.vault_id_undashed

    from ..vault.diagnostics.debug_bundle import write_debug_bundle, DebugBundleError
    from ..vault.ops.integrity import (
        IntegrityReport, run_full_check, run_quick_check,
    )

    maintenance_tab = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=12,
        margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
    )
    maintenance_tab.append(Gtk.Label(
        label="Diagnostics",
        xalign=0, css_classes=["title-3"],
    ))
    maintenance_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
    maintenance_tab.append(maintenance_status)

    # Debug bundle
    maintenance_tab.append(Gtk.Label(
        label="Download debug bundle",
        xalign=0, css_classes=["title-4"], margin_top=12,
    ))
    maintenance_tab.append(Gtk.Label(
        label=(
            "Packages a redacted snapshot of vault config, local index "
            "schema, binding states, and the tail of the vault.log file "
            "into a ZIP. The bundle is scrubbed for forbidden patterns "
            "before it lands on disk; if any leak survives the scrub, "
            "the export is refused."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))
    debug_bundle_btn = Gtk.Button(
        label="Download debug bundle…", css_classes=["pill"],
    )
    debug_bundle_btn.set_halign(Gtk.Align.START)
    maintenance_tab.append(debug_bundle_btn)

    def on_download_debug_bundle(_btn) -> None:
        dlg = Gtk.FileDialog()
        dlg.set_title("Save debug bundle")
        dlg.set_initial_name("vault-debug-bundle.zip")

        def on_chosen(file_dialog, result) -> None:
            try:
                gio_file = file_dialog.save_finish(result)
            except GLib.Error:
                return
            if gio_file is None:
                return
            path = gio_file.get_path()
            if not path:
                maintenance_status.set_label(
                    "Choose a destination for the bundle."
                )
                return
            _do_write_bundle(Path(path))

        dlg.save(parent=win, callback=on_chosen)

    def _do_write_bundle(destination: Path) -> None:
        debug_bundle_btn.set_sensitive(False)
        maintenance_status.set_label("Building debug bundle…")

        def worker() -> None:
            try:
                config.reload()
                config_dump = dict(config._data)
                from ..vault.state.local_index import VaultLocalIndex
                local_index = VaultLocalIndex(config_dir)
                activity_log = config_dir / "logs" / "vault.log"
                out = write_debug_bundle(
                    destination,
                    config=config_dump,
                    db_path=local_index.db_path,
                    activity_log_path=(
                        activity_log if activity_log.exists() else None
                    ),
                )
            except DebugBundleError as exc:
                msg = str(exc)

                def fail() -> bool:
                    debug_bundle_btn.set_sensitive(True)
                    maintenance_status.set_label(
                        f"Debug bundle refused: {msg}"
                    )
                    return False
                GLib.idle_add(fail)
                return
            except Exception as exc:  # noqa: BLE001
                msg = humanize(exc)

                def fail() -> bool:
                    debug_bundle_btn.set_sensitive(True)
                    maintenance_status.set_label(
                        f"Debug bundle failed: {msg}"
                    )
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                debug_bundle_btn.set_sensitive(True)
                maintenance_status.set_label(
                    f"Debug bundle saved to {out}."
                )
                return False
            GLib.idle_add(succeed)
        threading.Thread(target=worker, daemon=True).start()

    debug_bundle_btn.connect("clicked", on_download_debug_bundle)

    # Integrity check
    maintenance_tab.append(Gtk.Label(
        label="Check integrity",
        xalign=0, css_classes=["title-4"], margin_top=12,
    ))
    maintenance_tab.append(Gtk.Label(
        label=(
            "Quick: re-fetch head manifest, verify the parent-revision "
            "chain links cleanly, and confirm every referenced chunk "
            "is present on the relay. Full: also AEAD-decrypts each "
            "retained revision to surface bit-rot in older history."
        ),
        xalign=0, wrap=True, css_classes=["dim-label"],
    ))
    integrity_buttons = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
    )
    quick_check_btn = Gtk.Button(label="Quick check", css_classes=["pill"])
    full_check_btn = Gtk.Button(label="Full check", css_classes=["pill"])
    integrity_buttons.append(quick_check_btn)
    integrity_buttons.append(full_check_btn)
    maintenance_tab.append(integrity_buttons)

    integrity_report_label = Gtk.Label(
        xalign=0, wrap=True, selectable=True,
    )
    maintenance_tab.append(integrity_report_label)

    def _format_integrity_report(report: IntegrityReport) -> str:
        head = (
            f"{report.scope.title()} check: "
            f"{report.revisions_checked} revision(s), "
            f"{report.chunks_checked} chunk(s) checked. "
        )
        if report.ok:
            return head + "No issues found."
        head += f"{len(report.broken)} issue(s):"
        issue_lines = []
        for issue in report.broken[:20]:
            bits = [issue.kind, issue.target]
            if issue.detail:
                bits.append(issue.detail)
            issue_lines.append("  • " + " — ".join(bits))
        if len(report.broken) > 20:
            issue_lines.append(
                f"  …and {len(report.broken) - 20} more."
            )
        return head + "\n" + "\n".join(issue_lines)

    def _run_integrity_check(*, full: bool) -> None:
        quick_check_btn.set_sensitive(False)
        full_check_btn.set_sensitive(False)
        integrity_report_label.set_label("")
        scope = "full" if full else "quick"
        maintenance_status.set_label(
            f"Running {scope} integrity check…"
        )

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
                    if full:
                        # F-508: wire the real chunk + manifest decrypters
                        # so "Full check" actually walks bytes (the
                        # earlier call was missing required kwargs and
                        # the worker silently surfaced a TypeError).
                        from ..vault.ui.browser_model import (
                            decrypt_manifest as _decrypt_manifest_envelope,
                        )
                        from ..vault.download import _decrypt_chunk

                        def _full_decrypt_chunk(folder, entry, version, chunk, encrypted):
                            return _decrypt_chunk(
                                vault=vault,
                                remote_folder_id=str(
                                    folder.get("remote_folder_id") or ""
                                ),
                                file_id=str(entry.get("entry_id") or ""),
                                version_id=str(version.get("version_id") or ""),
                                chunk=chunk,
                                encrypted=encrypted,
                            )

                        def _full_fetch_chunk(vault_id, access_secret, chunk_id):
                            return relay.get_chunk(vault_id, access_secret, chunk_id)

                        def _full_decrypt_envelope(envelope):
                            return _decrypt_manifest_envelope(vault, envelope)

                        report = run_full_check(
                            vault=vault, relay=relay,
                            decrypt_chunk=_full_decrypt_chunk,
                            fetch_chunk=_full_fetch_chunk,
                            decrypt_manifest_envelope=_full_decrypt_envelope,
                        )
                    else:
                        report = run_quick_check(vault=vault, relay=relay)
                finally:
                    vault.close()
            except Exception as exc:  # noqa: BLE001
                msg = humanize(exc)

                def fail() -> bool:
                    quick_check_btn.set_sensitive(True)
                    full_check_btn.set_sensitive(True)
                    maintenance_status.set_label(
                        f"Integrity check failed: {msg}"
                    )
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                quick_check_btn.set_sensitive(True)
                full_check_btn.set_sensitive(True)
                summary = (
                    "✓ No issues" if report.ok
                    else f"⚠ {len(report.broken)} issue(s)"
                )
                maintenance_status.set_label(
                    f"{scope.title()} check: {summary}."
                )
                integrity_report_label.set_label(
                    _format_integrity_report(report)
                )
                return False
            GLib.idle_add(succeed)
        threading.Thread(target=worker, daemon=True).start()

    quick_check_btn.connect("clicked", lambda _b: _run_integrity_check(full=False))
    full_check_btn.connect("clicked", lambda _b: _run_integrity_check(full=True))
    # Disable integrity checks when no vault is loaded.
    if not vault_id_undashed:
        quick_check_btn.set_sensitive(False)
        full_check_btn.set_sensitive(False)
        maintenance_status.set_label(
            "Connect a vault before running integrity checks."
        )

    return maintenance_tab
