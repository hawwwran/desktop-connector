"""QuotaMixin — 507 quota-exceeded routing + eviction worker.

``_handle_quota_exceeded`` is the entry point that uploads' worker
threads call after catching ``VaultQuotaExceededError``; it either
opens the eviction prompt or paints the terminal "vault full"
banner. ``_run_eviction_pass`` is the worker that actually runs the
§D2 pipeline.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from ..vault.binding.lifecycle import SyncCancelledError
from ..vault.error_messages import humanize
from ..vault.relay_errors import VaultQuotaExceededError
from ..vault.binding.runtime import (
    create_vault_relay,
    open_local_vault_from_grant,
)
from ..vault_upload import describe_quota_exceeded


class QuotaMixin:
    """T6.6/T7.5 — 507 routing + eviction pass."""

    def _handle_quota_exceeded(
        self, exc: VaultQuotaExceededError, *, action: str,
    ) -> None:
        """T6.6 + T7.5: route a 507 into either the eviction prompt or
        the vault-full banner depending on ``eviction_available``."""
        info = describe_quota_exceeded(exc)
        if info["eviction_available"]:
            if self.quota_banner is not None:
                self.quota_banner.set_revealed(False)
            dlg = Adw.AlertDialog(
                heading=info["heading"],
                body=info["body"],
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("evict", info["primary_action_label"])
            # F-U04: eviction is irreversible (history is dropped),
            # so Enter must default to Cancel. The action button is
            # rendered destructive to match brand guidance.
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            dlg.set_response_appearance(
                "evict", Adw.ResponseAppearance.DESTRUCTIVE,
            )

            def on_response(_dialog, response: str) -> None:
                if response == "evict":
                    delta = max(1, exc.used_bytes - exc.quota_bytes + 1)
                    self._run_eviction_pass(action=action, target_bytes=delta)
                else:
                    self._set_status(
                        f"{action} paused — vault is full ({info['percent']}%).",
                        "error",
                    )
            dlg.connect("response", on_response)
            dlg.present(self.win)
            return

        # No history left → terminal sync-stop banner per §D2 step 4.
        if self.quota_banner is not None:
            self.quota_banner.set_title(info["body"])
            self.quota_banner.set_button_label(info["primary_action_label"])
            self.quota_banner.set_revealed(True)
        self._set_status(
            f"{action} stopped: vault full and no backup history remains.",
            "error",
        )

    def _run_eviction_pass(self, *, action: str, target_bytes: int) -> None:
        """T7.5: run the §D2 eviction pipeline in a worker thread."""
        vault_id = self._resolve_vault_id()
        if not vault_id:
            self._set_status("No local vault is connected.", "error")
            return

        if self.refresh_btn is not None:
            self.refresh_btn.set_sensitive(False)
        cancel_event = threading.Event()
        self._arm_cancel(cancel_event)
        if self.progress_bar is not None:
            self.progress_bar.set_fraction(0.0)
            self.progress_bar.set_text("Reclaiming space...")
        self._set_status(
            f"{action}: running eviction to free {target_bytes} bytes...",
        )

        def worker() -> None:
            try:
                from ..vault_eviction import eviction_pass

                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    current_manifest = vault.fetch_manifest(
                        relay, local_index=self.local_index,
                    )
                    device_id = str(getattr(self.config, "device_id", "") or "0" * 32)
                    result = eviction_pass(
                        vault=vault, relay=relay,
                        manifest=current_manifest,
                        author_device_id=device_id,
                        target_bytes_to_free=target_bytes,
                        local_index=self.local_index,
                        should_continue=lambda: not cancel_event.is_set(),
                    )
                finally:
                    vault.close()
            except SyncCancelledError:
                def cancelled() -> bool:
                    self._disarm_cancel()
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._set_status("Eviction cancelled.")
                    return False
                GLib.idle_add(cancelled)
                return
            except Exception as exc:
                error_message = humanize(exc)

                def fail() -> bool:
                    self._disarm_cancel()
                    if self.refresh_btn is not None:
                        self.refresh_btn.set_sensitive(True)
                    self._set_status(f"Eviction failed: {error_message}", "error")
                    return False
                GLib.idle_add(fail)
                return

            def succeed() -> bool:
                self.state.manifest = result.manifest
                self.state.selected_file = None
                self._disarm_cancel()
                if self.refresh_btn is not None:
                    self.refresh_btn.set_sensitive(True)
                if result.no_more_candidates:
                    if self.quota_banner is not None:
                        self.quota_banner.set_title(
                            "Vault is full and no backup history remains. "
                            "Sync is stopped. Free space by deleting files, "
                            "or export and migrate to a relay with more capacity."
                        )
                        self.quota_banner.set_button_label("Open vault settings")
                        self.quota_banner.set_revealed(True)
                    self._render_all(
                        f"Eviction stopped — no more candidates. "
                        f"Freed {result.bytes_freed} bytes.",
                        "error",
                    )
                else:
                    self._render_all(
                        f"Eviction freed {result.bytes_freed} bytes "
                        f"({result.chunks_freed} chunks). "
                        f"Try {action.lower()} again.",
                        "success",
                    )
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()
