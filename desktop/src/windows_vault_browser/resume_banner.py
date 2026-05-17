"""ResumeBannerMixin — interrupted-upload resume banner.

The banner is constructed by ``LayoutMixin._build_breadcrumb_and_status``;
this mixin owns the state-driven refresh and the two button handlers
(Resume → kick off the resume worker; Cancel → discard sessions).
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from ..vault.binding.lifecycle import SyncCancelledError
from ..vault.error_messages import humanize
from ..vault.binding.runtime import (
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault.upload import (
    clear_session,
    default_upload_resume_dir,
    list_resumable_sessions,
)

log = logging.getLogger(__name__)


class ResumeBannerMixin:
    """Refresh + Resume + Cancel for the interrupted-upload banner."""

    def _refresh_resume_banner(self, vault_id: str) -> None:
        try:
            sessions = list_resumable_sessions(
                vault_id, default_upload_resume_dir(),
            )
        except Exception:
            sessions = []
        self.state.resume_sessions = sessions
        if self.resume_banner_box is None or self.resume_banner_label is None:
            return
        if not sessions:
            self.resume_banner_box.set_visible(False)
            return
        count = len(sessions)
        label = (
            "1 upload was interrupted — click Resume to finish it, "
            "or Cancel to discard."
            if count == 1
            else f"{count} uploads were interrupted — click Resume to "
                 "finish them, or Cancel to discard."
        )
        self.resume_banner_label.set_label(label)
        self.resume_banner_box.set_visible(True)

    def _on_resume_cancel_clicked(self, _btn: Gtk.Button) -> None:
        """Discard the saved upload sessions so the banner stops appearing.

        The on-disk session JSON is removed via ``clear_session``;
        the local source file is untouched and the relay-side chunks
        the upload had already PUT stay until eviction or retention
        claims them. Re-uploading the same file later is a fresh
        ``upload_file`` call — the relay's hash-equality dedup means
        any chunks already stored come back as 200 OK no-ops.
        """
        sessions = list(self.state.resume_sessions or [])
        if not sessions:
            return
        cache_dir = default_upload_resume_dir()
        cleared = 0
        for session in sessions:
            try:
                clear_session(session.session_id, cache_dir)
                cleared += 1
            except Exception:
                log.exception(
                    "vault.browser.resume_cancel.clear_session_failed "
                    "session_id=%s",
                    getattr(session, "session_id", "?"),
                )
        self.state.resume_sessions = []
        if self.resume_banner_box is not None:
            self.resume_banner_box.set_visible(False)
        if cleared:
            noun = "session" if cleared == 1 else "sessions"
            self._set_status(
                f"Discarded {cleared} interrupted upload {noun}.",
                "dim-label",
            )

    def _start_resume_pending(self, _btn=None) -> None:
        sessions = list(self.state.resume_sessions or [])
        if not sessions:
            return
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        if self.refresh_btn is not None:
            self.refresh_btn.set_sensitive(False)
        if self.upload_btn is not None:
            self.upload_btn.set_sensitive(False)
        if self.upload_folder_btn is not None:
            self.upload_folder_btn.set_sensitive(False)
        if self.resume_banner_box is not None:
            self.resume_banner_box.set_visible(False)
        cancel_event = threading.Event()
        self._arm_cancel(cancel_event)
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text("Resuming uploads...")
        self._set_status(
            f"Resuming {len(sessions)} interrupted upload(s)...",
        )

        def worker() -> None:
            from ..vault.upload import resume_upload

            completed = 0
            failed = 0
            cancelled_count = 0
            last_manifest = self.state.manifest
            try:
                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    for session in sessions:
                        if cancel_event.is_set():
                            break
                        try:
                            current_manifest = vault.fetch_unified_manifest(
                                relay, local_index=self.local_index,
                            )
                            result = resume_upload(
                                vault=vault,
                                relay=relay,
                                manifest=current_manifest,
                                session=session,
                                local_index=self.local_index,
                                should_continue=lambda: not cancel_event.is_set(),
                            )
                            last_manifest = result.manifest
                            completed += 1
                        except SyncCancelledError:
                            cancelled_count += 1
                            break
                        except Exception:
                            failed += 1
                finally:
                    vault.close()
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    upload_dest = self._resolve_upload_destination()
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    if self.upload_btn is not None:
                        self.upload_btn.set_sensitive(upload_dest is not None)
                    if self.upload_folder_btn is not None:
                        self.upload_folder_btn.set_sensitive(upload_dest is not None)
                    self._set_status(f"Resume failed: {error_message}", "error")
                    self._refresh_resume_banner(vault_id)
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self._disarm_cancel()
                upload_dest = self._resolve_upload_destination()
                if self.refresh_btn is not None:
                    self.refresh_btn.set_sensitive(True)
                if self.upload_btn is not None:
                    self.upload_btn.set_sensitive(upload_dest is not None)
                if self.upload_folder_btn is not None:
                    self.upload_folder_btn.set_sensitive(upload_dest is not None)
                if last_manifest is not None:
                    self.state.manifest = last_manifest
                self.state.selected_file = None
                self._render_all()
                if cancelled_count > 0:
                    self._set_status(
                        f"Resume cancelled. {completed} upload(s) finished, "
                        f"{len(sessions) - completed - failed} pending.",
                    )
                elif failed == 0:
                    self._set_status(
                        f"Resumed {completed} upload(s).", "success",
                    )
                else:
                    self._set_status(
                        f"Resumed {completed} upload(s); {failed} failed "
                        "(will retry next time).",
                        "error",
                    )
                self._refresh_resume_banner(vault_id)
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()
