"""GTK4 import wizard for the Vault (T8.5).

A linear ``Gtk.Stack`` with five pages — pick file, enter passphrase,
review preview, run import, summary. The wizard delegates all the
heavy lifting to :mod:`vault_import_runner`; this module is just the
GTK glue, page transitions, and worker-thread plumbing.

Conflict-resolution UI (per-folder mode picker + "apply to remaining")
is not yet wired here: T8.4's `merge_import_into` accepts an
`ImportMergeResolution` so we'll layer the conflict UX on the same
backbone in a follow-up. For T8.5 the wizard always uses the §D9
default (`rename`), which is the conservative choice — bundle copies
land beside the active vault's files instead of overwriting them.
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

from .brand import (
    apply_brand_css,
    apply_pointer_cursors,
    apply_theme_mode_from_config_dir,
)
from .vault.binding.lifecycle import SyncCancelledError
from .vault.state.local_index import VaultLocalIndex
from .vault.error_messages import humanize
from .vault.export.bundle import ExportError
from .vault.export.reminder import normalize_cadence
from .vault.import_.bundle import (
    FolderConflictBatch,
    ImportMergeResolution,
    find_conflict_batches,
)
from .vault.binding.runtime import create_vault_relay, open_local_vault_from_grant
from .windows_common import _make_app
from .windows_vault.fresh_unlock_prompt import require_fresh_unlock_or_prompt


def show_vault_import(config_dir: Path, vault_id_override: str | None = None) -> None:
    """Top-level entry point for ``--gtk-window=vault-import``.

    ``vault_id_override`` (F-U14): optional 12-char canonical vault id;
    when present every ``local_vault_id()`` call returns it instead of
    re-reading ``config['vault']['last_known_id']``. The wizard merges
    the bundle into the active vault, so the override pins which vault
    that is even if a future multi-vault tray opens several wizards
    simultaneously.
    """
    from .config import Config
    from .vault.ui.window_args import resolve_active_vault_id

    config = Config(config_dir)
    local_index = VaultLocalIndex(config_dir)
    app = _make_app()

    state: dict = {
        "bundle_path": None,
        "passphrase": None,
        "preview": None,
        "bundle_contents": None,
        "bundle_manifest": None,
        "active_manifest": None,
        "result": None,
        # §5.H2: per-folder conflict resolution state.
        #   "conflicts": list[FolderConflictBatch] computed during
        #     preview load. Empty list when no conflicts — the
        #     wizard skips the conflicts page and goes straight to
        #     fresh-unlock + progress.
        #   "resolution": dict[folder_id → "rename"|"overwrite"|"skip"]
        #     filled in on the conflicts page; threaded into
        #     ``ImportMergeResolution`` at run_import time.
        "conflicts": [],
        "resolution": {},
    }

    def local_vault_id() -> str:
        return resolve_active_vault_id(config, vault_id_override)

    def on_activate(app: Adw.Application) -> None:
        apply_brand_css()
        apply_theme_mode_from_config_dir(config_dir)

        win = Adw.ApplicationWindow(
            application=app,
            title="Import vault bundle",
            default_width=720,
            default_height=520,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        toolbar.set_content(stack)

        def go_to(name: str) -> None:
            stack.set_visible_child_name(name)

        # ---- Page 1: pick file + passphrase ---------------------------
        pick_box = _vbox(margin=24, spacing=14)
        pick_box.append(Gtk.Label(
            label="Import a vault export bundle",
            xalign=0,
            css_classes=["title-2"],
        ))
        pick_box.append(Gtk.Label(
            label=(
                "Choose a .dcvault file and enter the export passphrase. "
                "Imports merge into the active vault per the §D9 rules; "
                "conflicts default to a (conflict imported) rename."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))

        file_row = Adw.ActionRow(title="Bundle file", subtitle="No file selected")
        pick_btn = Gtk.Button(label="Choose…", css_classes=["pill"])
        file_row.add_suffix(pick_btn)
        pick_box.append(file_row)

        # Single passphrase entry — no confirm field. The create wizard
        # confirms because a typo there silently locks the user out of
        # their own vault. Import is the inverse: the user is typing
        # back a passphrase they already chose, and a typo just fails
        # the bundle's AEAD with a visible "Bundle decryption failed"
        # error. They retype and try again. A confirm field would add
        # friction to the recovery path without preventing any
        # silent-lockout class of error.
        passphrase_entry = Gtk.PasswordEntry(placeholder_text="Export passphrase")
        passphrase_entry.set_show_peek_icon(True)
        pick_box.append(passphrase_entry)

        pick_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        pick_box.append(pick_status)

        pick_actions = _hbox(spacing=8)
        pick_actions.set_halign(Gtk.Align.END)
        cancel_btn_1 = Gtk.Button(label="Cancel", css_classes=["pill"])
        open_btn = Gtk.Button(
            label="Open bundle",
            css_classes=["pill", "suggested-action"],
        )
        open_btn.set_sensitive(False)
        pick_actions.append(cancel_btn_1)
        pick_actions.append(open_btn)
        pick_box.append(pick_actions)

        stack.add_named(pick_box, "pick")

        # ---- Page 2: preview ------------------------------------------
        preview_box = _vbox(margin=24, spacing=12)
        preview_box.append(Gtk.Label(
            label="Bundle preview",
            xalign=0,
            css_classes=["title-2"],
        ))
        preview_grid = Gtk.Grid(column_spacing=18, row_spacing=6)
        preview_box.append(preview_grid)
        preview_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        preview_box.append(preview_status)
        preview_actions = _hbox(spacing=8)
        preview_actions.set_halign(Gtk.Align.END)
        cancel_btn_2 = Gtk.Button(label="Cancel", css_classes=["pill"])
        import_btn = Gtk.Button(
            label="Import",
            css_classes=["pill", "suggested-action"],
        )
        preview_actions.append(cancel_btn_2)
        preview_actions.append(import_btn)
        preview_box.append(preview_actions)
        stack.add_named(preview_box, "preview")

        # ---- Page 3: per-folder conflict resolution (§5.H2) ---------
        #
        # Inserted between Preview and Progress. Only shown if
        # ``find_conflict_batches`` returned a non-empty list for the
        # bundle vs the active vault; otherwise ``on_import`` skips
        # straight to fresh-unlock + progress.
        #
        # Layout: one card per ``FolderConflictBatch``. Each card has
        # the folder display name, the conflict count, three radio
        # buttons (Rename — default, conservative; Overwrite; Skip)
        # and an "Apply to remaining" link that copies the chosen
        # mode into every still-undecided folder below. Continue
        # stays disabled until every folder has a pick.
        conflicts_box = _vbox(margin=24, spacing=12)
        conflicts_box.append(Gtk.Label(
            label="Choose how to resolve name conflicts",
            xalign=0, css_classes=["title-2"],
        ))
        conflicts_box.append(Gtk.Label(
            label=(
                "These folders have file names that are live on both "
                "sides. Pick a resolution per folder. Bundle versions "
                "are always preserved as history — Skip and Overwrite "
                "only differ in which side keeps the current head."
            ),
            xalign=0, wrap=True, css_classes=["dim-label"],
        ))
        conflicts_scroller = Gtk.ScrolledWindow()
        conflicts_scroller.set_vexpand(True)
        conflicts_scroller.set_min_content_height(280)
        conflicts_list = _vbox(spacing=10)
        conflicts_scroller.set_child(conflicts_list)
        conflicts_box.append(conflicts_scroller)

        conflicts_actions = _hbox(spacing=8)
        conflicts_actions.set_halign(Gtk.Align.END)
        conflicts_back_btn = Gtk.Button(label="Back", css_classes=["pill"])
        conflicts_continue_btn = Gtk.Button(
            label="Continue",
            css_classes=["pill", "suggested-action"],
        )
        conflicts_continue_btn.set_sensitive(False)
        conflicts_actions.append(conflicts_back_btn)
        conflicts_actions.append(conflicts_continue_btn)
        conflicts_box.append(conflicts_actions)
        stack.add_named(conflicts_box, "conflicts")

        # ---- Page 4: progress -----------------------------------------
        progress_box = _vbox(margin=24, spacing=14)
        progress_box.append(Gtk.Label(
            label="Importing…",
            xalign=0, css_classes=["title-2"],
        ))
        progress_label = Gtk.Label(xalign=0, wrap=True)
        progress_bar = Gtk.ProgressBar(show_text=True)
        progress_box.append(progress_label)
        progress_box.append(progress_bar)
        # F-U03: Cancel during import. Cancel-before-publish is safe;
        # any chunks already PUT to the relay become orphans cleaned
        # up by the next eviction housekeeping pass per §D2.
        progress_actions = _hbox(spacing=8)
        progress_actions.set_halign(Gtk.Align.END)
        progress_cancel_btn = Gtk.Button(label="Cancel", css_classes=["pill"])
        progress_actions.append(progress_cancel_btn)
        progress_box.append(progress_actions)
        stack.add_named(progress_box, "progress")
        import_cancel_event: dict[str, "threading.Event | None"] = {"event": None}

        def _on_progress_cancel(_btn: Gtk.Button) -> None:
            event = import_cancel_event["event"]
            if event is None:
                return
            event.set()
            progress_cancel_btn.set_sensitive(False)
            progress_cancel_btn.set_label("Cancelling…")
        progress_cancel_btn.connect("clicked", _on_progress_cancel)

        # ---- Page 4: summary ------------------------------------------
        summary_box = _vbox(margin=24, spacing=12)
        summary_title = Gtk.Label(xalign=0, css_classes=["title-2"])
        summary_body = Gtk.Label(xalign=0, wrap=True)
        summary_actions = _hbox(spacing=8)
        summary_actions.set_halign(Gtk.Align.END)
        close_btn = Gtk.Button(label="Close", css_classes=["pill", "suggested-action"])
        summary_actions.append(close_btn)
        summary_box.append(summary_title)
        summary_box.append(summary_body)
        summary_box.append(summary_actions)
        stack.add_named(summary_box, "summary")

        # ---------------------------------------------------------------

        def _wipe_passphrase() -> None:
            """Review §5.M1 — best-effort passphrase wipe.

            Python ``str`` is immutable, so the bytes can't be zeroed
            in-place; the heap allocation stays until GC collects it.
            But we can drop our explicit references immediately so the
            string isn't pinned by the wizard's ``state`` dict for the
            rest of the window's lifetime. The entry widget's buffer
            is also cleared so the visible field doesn't carry the
            text past the operation.
            """
            state["passphrase"] = ""
            state.pop("passphrase", None)
            try:
                passphrase_entry.set_text("")
            except Exception:  # noqa: BLE001
                # Wizard already torn down; nothing to clear.
                pass

        def cancel(_btn=None) -> None:
            _wipe_passphrase()
            win.close()

        def update_open_btn_sensitive() -> None:
            open_btn.set_sensitive(
                state["bundle_path"] is not None
                and bool(passphrase_entry.get_text())
            )

        def on_pick(_btn) -> None:
            dlg = Gtk.FileDialog()
            dlg.set_title("Select vault export bundle")

            def on_file_chosen(file_dialog, result) -> None:
                try:
                    gio_file = file_dialog.open_finish(result)
                except GLib.Error:
                    return
                if gio_file is None:
                    return
                path_text = gio_file.get_path()
                if not path_text:
                    return
                state["bundle_path"] = Path(path_text)
                file_row.set_subtitle(path_text)
                update_open_btn_sensitive()

            dlg.open(parent=win, callback=on_file_chosen)

        def on_passphrase_changed(_entry) -> None:
            update_open_btn_sensitive()

        def on_open(_btn) -> None:
            state["passphrase"] = passphrase_entry.get_text()
            vault_id = local_vault_id()
            if not vault_id:
                pick_status.set_label("No active vault on this device.")
                return
            open_btn.set_sensitive(False)
            pick_status.set_label("Opening bundle…")

            def worker() -> None:
                try:
                    from .vault.import_.runner import open_bundle_for_preview

                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        active_manifest = vault.fetch_unified_manifest(relay, local_index=local_index)
                        # Review §5.H1: pull the active vault's
                        # genesis_fingerprint from its decrypted
                        # header so decide_import_action runs the
                        # cryptographic identity gate, not just the
                        # vault_id match. Pre-fix this was hard-
                        # coded to None and the gate short-circuited
                        # on vault_id alone — an attacker who could
                        # forge a vault_id collision bypassed the
                        # whole anchor.
                        active_fp = _safe_genesis_fingerprint(vault, relay)
                        state["active_genesis_fingerprint"] = active_fp
                        # Review §5.H4: thread ``relay`` so
                        # ``open_bundle_for_preview`` does the
                        # ``batch_head_chunks`` round-trip and the
                        # preview's "0 of N chunks already on this
                        # relay" line shows the real number BEFORE
                        # the user clicks Import. Pre-fix the head
                        # call lived inside ``run_import``, so the
                        # preview always read zero.
                        contents, bundle_manifest, preview = open_bundle_for_preview(
                            vault=vault,
                            relay=relay,
                            bundle_path=state["bundle_path"],
                            passphrase=state["passphrase"],
                            active_manifest=active_manifest,
                            active_genesis_fingerprint=active_fp,
                            bundle_genesis_fingerprint=_safe_bundle_genesis_fingerprint(
                                state["bundle_path"], state["passphrase"], vault.vault_id,
                            ),
                        )
                    finally:
                        vault.close()
                except ExportError as exc:
                    def fail() -> bool:
                        open_btn.set_sensitive(True)
                        pick_status.set_label(f"Could not open bundle: {exc.code}: {exc}")
                        return False
                    GLib.idle_add(fail)
                    return
                except Exception as exc:
                    error_message = humanize(exc)

                    def fail() -> bool:
                        open_btn.set_sensitive(True)
                        pick_status.set_label(f"Could not open bundle: {error_message}")
                        return False
                    GLib.idle_add(fail)
                    return

                def show_preview() -> bool:
                    state["bundle_contents"] = contents
                    state["bundle_manifest"] = bundle_manifest
                    state["preview"] = preview
                    state["active_manifest"] = active_manifest
                    # §5.H2: surface per-folder conflict batches so the
                    # post-Preview page can ask for a per-folder mode
                    # before run_import. Empty list = bundle has no
                    # live-vs-live collisions; wizard skips the page.
                    if active_manifest is not None:
                        state["conflicts"] = find_conflict_batches(
                            active_manifest=active_manifest,
                            bundle_manifest=bundle_manifest,
                        )
                    else:
                        state["conflicts"] = []
                    state["resolution"] = {}
                    _render_preview(preview_grid, preview)
                    if preview.fingerprint_status == "different_vault":
                        preview_status.set_label(
                            "This bundle is for a *different* vault. Import is "
                            "refused — switch active vault first."
                        )
                        import_btn.set_sensitive(False)
                    else:
                        preview_status.set_label("")
                        import_btn.set_sensitive(True)
                    go_to("preview")
                    return False
                GLib.idle_add(show_preview)

            threading.Thread(target=worker, daemon=True).start()

        def on_import(_btn) -> None:
            # §5.H2: route through the conflict-resolution page when
            # the bundle has live-vs-live name collisions. The page's
            # Continue button calls ``_after_conflict_resolution``
            # which re-runs the fresh-unlock gate before the actual
            # import worker fires.
            if state.get("conflicts"):
                _render_conflicts_page()
                go_to("conflicts")
                return
            require_fresh_unlock_or_prompt(
                win,
                config=config,
                operation_label="merge bundle into active vault",
                on_success=_start_import,
            )

        def _render_conflicts_page() -> None:
            """Rebuild the per-folder card list from ``state['conflicts']``.

            Called each time the page is presented; each card carries
            three radio CheckButtons (grouped) bound to the same
            ``state['resolution'][folder_id]`` slot. The "Apply to
            remaining" button copies the chosen mode into every
            still-undecided folder below.
            """
            child = conflicts_list.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                conflicts_list.remove(child)
                child = nxt

            batches: list[FolderConflictBatch] = list(state.get("conflicts") or [])
            radio_groups: dict[str, dict[str, Gtk.CheckButton]] = {}

            def _refresh_continue() -> None:
                conflicts_continue_btn.set_sensitive(
                    all(b.remote_folder_id in state["resolution"] for b in batches)
                )

            for idx, batch in enumerate(batches):
                card = _vbox(margin=10, spacing=6)
                card.add_css_class("card")
                title = batch.display_name or batch.remote_folder_id[:12]
                card.append(Gtk.Label(
                    label=title, xalign=0, css_classes=["heading"],
                ))
                card.append(Gtk.Label(
                    label=(
                        f"{len(batch.conflicting_paths)} conflicting file "
                        f"name{'s' if len(batch.conflicting_paths) != 1 else ''}."
                    ),
                    xalign=0, wrap=True, css_classes=["dim-label"],
                ))

                radios: dict[str, Gtk.CheckButton] = {}
                first: Gtk.CheckButton | None = None
                for mode, label, hint in (
                    ("rename",
                     "Rename (default — keep both)",
                     "Bundle file lands at '<path> (conflict imported …)'; "
                     "active head untouched."),
                    ("overwrite",
                     "Overwrite — bundle wins",
                     "Bundle version becomes the new current head; active "
                     "version preserved as history."),
                    ("skip",
                     "Skip — active wins (entire folder)",
                     "Active head stays current; bundle versions archived "
                     "as history. Whole folder is skipped from new-head "
                     "writes per spec §17."),
                ):
                    btn = Gtk.CheckButton(label=label)
                    btn.update_property(
                        [Gtk.AccessibleProperty.LABEL],
                        [f"{title} — {label}"],
                    )
                    if first is None:
                        first = btn
                    else:
                        btn.set_group(first)
                    radios[mode] = btn
                    card.append(btn)
                    card.append(Gtk.Label(
                        label=hint, xalign=0, wrap=True,
                        css_classes=["dim-label"],
                    ))

                def _on_toggled(_button, *, folder_id=batch.remote_folder_id,
                                radio_map=radios) -> None:
                    for mode_token, candidate in radio_map.items():
                        if candidate.get_active():
                            state["resolution"][folder_id] = mode_token
                            break
                    _refresh_continue()

                for btn in radios.values():
                    btn.connect("toggled", _on_toggled)

                # Pre-fill any prior pick (e.g. user clicked Back and
                # returned to this page) so the radio reflects state.
                pre_pick = state["resolution"].get(batch.remote_folder_id)
                if pre_pick is not None and pre_pick in radios:
                    radios[pre_pick].set_active(True)

                apply_remaining = Gtk.Button(
                    label="Apply this choice to remaining folders",
                    css_classes=["pill"],
                )
                apply_remaining.set_halign(Gtk.Align.START)

                def _on_apply_remaining(_b, *, idx=idx, folder_id=batch.remote_folder_id,
                                        radio_map=radios) -> None:
                    chosen: str | None = None
                    for mode_token, candidate in radio_map.items():
                        if candidate.get_active():
                            chosen = mode_token
                            break
                    if chosen is None:
                        return
                    for later in batches[idx + 1:]:
                        if later.remote_folder_id in state["resolution"]:
                            # Don't overwrite an explicit pick the
                            # operator already made — only fill in
                            # the still-blank ones below.
                            continue
                        state["resolution"][later.remote_folder_id] = chosen
                        later_radios = radio_groups.get(later.remote_folder_id)
                        if later_radios and chosen in later_radios:
                            later_radios[chosen].set_active(True)
                    _refresh_continue()

                apply_remaining.connect("clicked", _on_apply_remaining)
                card.append(apply_remaining)
                radio_groups[batch.remote_folder_id] = radios
                conflicts_list.append(card)

            _refresh_continue()

        def _after_conflict_resolution() -> None:
            require_fresh_unlock_or_prompt(
                win,
                config=config,
                operation_label="merge bundle into active vault",
                on_success=_start_import,
            )

        def _on_conflicts_back(_btn) -> None:
            go_to("preview")

        def _on_conflicts_continue(_btn) -> None:
            _after_conflict_resolution()

        conflicts_back_btn.connect("clicked", _on_conflicts_back)
        conflicts_continue_btn.connect("clicked", _on_conflicts_continue)

        def _start_import() -> None:
            import_btn.set_sensitive(False)
            progress_label.set_label("Uploading missing chunks…")
            progress_bar.set_fraction(0.0)
            progress_bar.set_text("")
            go_to("progress")

            def report(progress) -> None:
                def update() -> bool:
                    if progress.chunks_total > 0:
                        fraction = (progress.chunks_uploaded + progress.chunks_skipped) / max(
                            1, progress.chunks_total,
                        )
                        progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
                        progress_bar.set_text(
                            f"{progress.chunks_uploaded + progress.chunks_skipped}/"
                            f"{progress.chunks_total} chunks"
                        )
                    progress_label.set_label({
                        "uploading_chunks": "Uploading missing chunks…",
                        "publishing": "Publishing merged manifest…",
                        "done": "Finished.",
                    }.get(progress.phase, progress.phase))
                    return False
                GLib.idle_add(update)

            cancel_event = threading.Event()
            import_cancel_event["event"] = cancel_event
            progress_cancel_btn.set_sensitive(True)
            progress_cancel_btn.set_label("Cancel")

            def worker() -> None:
                try:
                    from .vault.import_.runner import run_import

                    vault_id = local_vault_id()
                    config.reload()
                    relay = create_vault_relay(config)
                    vault = open_local_vault_from_grant(config_dir, config, vault_id)
                    try:
                        active_manifest = vault.fetch_unified_manifest(relay, local_index=local_index)
                        device_id = str(getattr(config, "device_id", "") or "0" * 32)
                        # Review §5.H1: thread both genesis_fingerprint
                        # values into run_import so the identity gate
                        # runs end-to-end. open_bundle_for_preview
                        # already enforced the §D9 refuse on
                        # different-vault bundles — this is the
                        # belt-and-braces second check at the actual
                        # merge boundary.
                        active_fp_run = (
                            state.get("active_genesis_fingerprint")
                            or _safe_genesis_fingerprint(vault, relay)
                        )
                        # §5.H2: thread the per-folder picks from the
                        # conflict-resolution page. Empty dict =
                        # there were no conflicts, fall back to the
                        # spec-§D9 default (rename, conservative).
                        result = run_import(
                            vault=vault, relay=relay,
                            bundle_path=state["bundle_path"],
                            passphrase=state["passphrase"],
                            active_manifest=active_manifest,
                            resolution=ImportMergeResolution(
                                per_folder=dict(state.get("resolution") or {}),
                            ),
                            author_device_id=device_id,
                            active_genesis_fingerprint=active_fp_run,
                            bundle_genesis_fingerprint=_safe_bundle_genesis_fingerprint(
                                state["bundle_path"], state["passphrase"], vault.vault_id,
                            ),
                            progress=report,
                            local_index=local_index,
                            should_continue=lambda: not cancel_event.is_set(),
                        )
                    finally:
                        vault.close()
                except SyncCancelledError:
                    def cancelled() -> bool:
                        import_cancel_event["event"] = None
                        # Review §5.M1 — drop the passphrase on cancel.
                        _wipe_passphrase()
                        summary_title.set_label("Import cancelled")
                        summary_body.set_label(
                            "Import was cancelled before publish. Any chunks already "
                            "uploaded to the relay become orphans and are reclaimed by "
                            "the next eviction housekeeping pass."
                        )
                        go_to("summary")
                        return False
                    GLib.idle_add(cancelled)
                    return
                except Exception as exc:
                    error_message = humanize(exc)

                    def fail() -> bool:
                        import_cancel_event["event"] = None
                        # Review §5.M1 — drop the passphrase reference
                        # now that the import flow is done.
                        _wipe_passphrase()
                        summary_title.set_label("Import failed")
                        summary_body.set_label(error_message)
                        go_to("summary")
                        return False
                    GLib.idle_add(fail)
                    return

                def succeed() -> bool:
                    import_cancel_event["event"] = None
                    # Review §5.M1 — same terminal cleanup on success.
                    _wipe_passphrase()
                    state["result"] = result
                    if result.action == "refuse":
                        summary_title.set_label("Import refused")
                        summary_body.set_label(
                            "The bundle is for a different vault. Import was not "
                            "performed; switch active vault and try again."
                        )
                    else:
                        merge_summary = result.merge
                        renamed = (
                            len(merge_summary.renamed_paths) if merge_summary else 0
                        )
                        new = len(merge_summary.new_paths) if merge_summary else 0
                        overwritten = (
                            len(merge_summary.overwritten_paths) if merge_summary else 0
                        )
                        skipped = (
                            len(merge_summary.skipped_paths) if merge_summary else 0
                        )
                        summary_title.set_label("Import complete")
                        summary_body.set_label(
                            f"Uploaded {result.chunks_uploaded} chunks, "
                            f"skipped {result.chunks_skipped}.\n"
                            f"New: {new}; renamed: {renamed}; "
                            f"overwritten: {overwritten}; skipped: {skipped}.\n"
                            "The browser shows the merged result on next refresh."
                        )
                    go_to("summary")
                    return False
                GLib.idle_add(succeed)

            threading.Thread(target=worker, daemon=True).start()

        pick_btn.connect("clicked", on_pick)
        passphrase_entry.connect("changed", on_passphrase_changed)
        open_btn.connect("clicked", on_open)
        cancel_btn_1.connect("clicked", cancel)
        cancel_btn_2.connect("clicked", cancel)
        import_btn.connect("clicked", on_import)
        close_btn.connect("clicked", cancel)

        go_to("pick")
        apply_pointer_cursors(win)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def _safe_genesis_fingerprint(vault, relay) -> str | None:
    """Best-effort read of the active vault's genesis_fingerprint
    from its decrypted header. Returns ``None`` on any failure so
    the wizard's identity gate falls back to vault_id-only matching
    (the pre-§5.H1 behaviour). Review §5.H1: this is the helper that
    plugs the previously-hard-coded ``None`` from the import flow."""
    try:
        header = vault.fetch_header_plaintext(relay)
    except Exception:  # noqa: BLE001
        return None
    fp = header.get("genesis_fingerprint")
    if not fp:
        return None
    return str(fp).strip().lower() or None


def _safe_bundle_genesis_fingerprint(
    bundle_path: Path, passphrase: str, vault_id: str,
) -> str | None:
    """Best-effort read of the bundle's header.genesis_fingerprint
    (review §5.H1). Returns ``None`` for legacy bundles that
    predate the field."""
    try:
        from .vault.export.bundle import read_export_bundle
        contents = read_export_bundle(
            bundle_path=Path(bundle_path),
            passphrase=passphrase,
            vault_id=vault_id,
        )
    except Exception:  # noqa: BLE001
        return None
    fp = getattr(contents.header, "genesis_fingerprint", None)
    if not fp:
        return None
    return str(fp).strip().lower() or None


def _vbox(*, margin: int = 0, spacing: int = 6) -> Gtk.Box:
    return Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=spacing,
        margin_top=margin, margin_bottom=margin,
        margin_start=margin, margin_end=margin,
    )


def _hbox(*, spacing: int = 6) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)


def _render_preview(grid: Gtk.Grid, preview) -> None:
    """Lay out the §gaps §17 preview fields into a 2-column grid."""
    child = grid.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        grid.remove(child)
        child = nxt

    rows = [
        ("Vault fingerprint",
         f"{preview.bundle_genesis_fingerprint or '(unknown)'} — {_fp_status_text(preview.fingerprint_status)}"),
        ("Source", preview.source_label),
        ("Vault size",
         f"{_format_bytes(preview.bundle_logical_size)} / "
         f"{_format_bytes(preview.bundle_ciphertext_size)} ciphertext"),
        ("Remote folders",
         f"{len(preview.folders)} ({', '.join(f.display_name for f in preview.folders[:10])}"
         + (f", … +{len(preview.folders) - 10} more" if len(preview.folders) > 10 else "")
         + ")"),
        ("History",
         f"{preview.current_files} current / {preview.total_versions} versions / {preview.tombstones} tombstones"),
        ("Conflicts with active vault", str(preview.conflicts)),
        ("Head impact",
         "Will change current head: yes" if preview.will_change_head else "Will change current head: no"),
        ("Bandwidth preview",
         f"{preview.chunks_already_on_relay} of {preview.chunks_total} chunks already on this relay (will skip)"),
    ]
    for row_index, (label, value) in enumerate(rows):
        key = Gtk.Label(label=label, xalign=0, css_classes=["dim-label"])
        val = Gtk.Label(label=value, xalign=0, wrap=True)
        val.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        grid.attach(key, 0, row_index, 1, 1)
        grid.attach(val, 1, row_index, 1, 1)


def _fp_status_text(status: str) -> str:
    return {
        "new_vault": "no active vault on this device",
        "matches_active": "matches active vault",
        "different_vault": "different vault",
    }.get(status, status)


def _format_bytes(value: int) -> str:
    size = max(0, int(value))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    return f"{amount:.1f} {unit}"
