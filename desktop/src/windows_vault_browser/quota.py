"""QuotaMixin — 507 quota-exceeded routing + eviction worker.

``_handle_quota_exceeded`` is the entry point that uploads' worker
threads call after catching ``VaultQuotaExceededError``. Three
mutually-exclusive paths drop out of the v1 eviction design
(ADR ``2026-05-18 — Eviction policy``):

1. **Alarm** (``used > quota``): suspends uploads, requires a fresh
   recovery-passphrase proof, then runs the destructive purge in
   ``mode="alarm"`` until ``used ≤ quota``. The relay can only report
   ``used > quota`` if the quota shrank below previously-stored bytes
   or the relay is tampering; passphrase gate keeps a casual local
   attacker from triggering mass deletion.
2. **Silent auto-purge** (``eviction_available=True`` and not alarm):
   no dialog, no passphrase. The status bar shows "Reclaiming space"
   while the destructive purge frees just enough for the failing
   upload to fit, then the user retries.
3. **No history** (``eviction_available=False``): terminal "vault is
   full and no backup history remains" banner — user must export or
   migrate to a relay with more capacity.
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
from ..vault.upload import describe_quota_exceeded
from ..vault.upload.constants import CHUNK_SIZE
from ..windows_vault.fresh_unlock_prompt import require_fresh_unlock_or_prompt


class QuotaMixin:
    """v1 — 507 routing across alarm / silent auto-purge / terminal."""

    def _handle_quota_exceeded(
        self, exc: VaultQuotaExceededError, *, action: str,
    ) -> None:
        """Route a 507 into alarm / silent auto-purge / terminal."""
        info = describe_quota_exceeded(exc)

        if info["alarm"]:
            self._present_alarm_dialog(exc, info, action=action)
            return

        if info["eviction_available"]:
            # Silent auto-purge — no dialog. Free enough for one
            # chunk's worth of room beyond current `used`; the
            # destructive iterator stops as soon as the upload fits,
            # so over-frees are bounded to one candidate's
            # ciphertext.
            if self.quota_banner is not None:
                self.quota_banner.set_revealed(False)
            delta = max(
                CHUNK_SIZE,
                info["used_bytes"] + CHUNK_SIZE - info["quota_bytes"] + 1,
            )
            self._set_status(
                f"{action} paused — vault is at {info['percent']}% of quota. "
                "Reclaiming space..."
            )
            self._run_eviction_pass(action=action, target_bytes=delta, mode="auto")
            return

        # No history left → terminal sync-stop banner.
        if self.quota_banner is not None:
            self.quota_banner.set_title(info["body"])
            self.quota_banner.set_button_label(info["primary_action_label"])
            self.quota_banner.set_revealed(True)
        self._set_status(
            f"{action} stopped: vault full and no backup history remains.",
            "error",
        )

    def _present_alarm_dialog(
        self,
        exc: VaultQuotaExceededError,
        info: dict,
        *,
        action: str,
    ) -> None:
        """Show the alarm banner + passphrase prompt, run the alarm
        cleanup on successful re-verification.

        The relay's ``used > quota`` signal is the unambiguous tamper /
        quota-shrink condition (the init-deny guard makes overflow
        impossible under normal operation). All uploads stay suspended
        until the cleanup completes — :meth:`_run_eviction_pass`
        handles the worker thread + UI state.
        """
        import logging
        logging.getLogger(__name__).warning(
            "vault.eviction.alarm_used_exceeds_quota used=%d quota=%d",
            info["used_bytes"], info["quota_bytes"],
        )
        if self.quota_banner is not None:
            self.quota_banner.set_title(info["body"])
            self.quota_banner.set_button_label(info["primary_action_label"])
            self.quota_banner.set_revealed(True)
        self._set_status(
            f"{action} suspended — vault quota cleanup needs approval.",
            "error",
        )

        # used - quota + 1 guarantees strictly-under after one full
        # sweep; the destructive iterator stops as soon as it crosses
        # that line, so over-purges are bounded to one candidate.
        delta = max(1, info["used_bytes"] - info["quota_bytes"] + 1)

        def on_success() -> None:
            self._run_eviction_pass(
                action=action, target_bytes=delta, mode="alarm",
            )

        def on_cancel() -> None:
            self._set_status(
                f"{action} suspended — open Vault Settings → Storage to "
                "approve cleanup, raise the quota, or migrate to a relay "
                "with more capacity.",
                "error",
            )

        require_fresh_unlock_or_prompt(
            self.win,
            config=self.config,
            operation_label="quota cleanup",
            on_success=on_success,
            on_cancel=on_cancel,
        )

    def _run_eviction_pass(
        self, *, action: str, target_bytes: int, mode: str = "auto",
    ) -> None:
        """Run the eviction pipeline in a worker thread.

        ``mode`` is passed through to
        :func:`desktop.src.vault.ops.eviction.eviction_pass` so the
        audit log distinguishes ``vault.eviction.auto_purged_oldest``
        from ``vault.eviction.alarm_purged_oldest``.
        """
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
        if mode == "alarm":
            self._set_status(
                f"{action}: running approved cleanup to bring vault back "
                f"under quota ({target_bytes} bytes)..."
            )
        else:
            self._set_status(
                f"{action}: reclaiming {target_bytes} bytes of space..."
            )

        def worker() -> None:
            try:
                from ..vault.ops.eviction import eviction_pass

                self.config.reload()
                relay = create_vault_relay(self.config)
                vault = open_local_vault_from_grant(
                    self.config_dir, self.config, vault_id,
                )
                try:
                    current_manifest = vault.fetch_unified_manifest(
                        relay, local_index=self.local_index,
                    )
                    device_id = str(getattr(self.config, "device_id", "") or "0" * 32)
                    result = eviction_pass(
                        vault=vault, relay=relay,
                        manifest=current_manifest,
                        author_device_id=device_id,
                        target_bytes_to_free=target_bytes,
                        mode=mode,
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
                    if self.quota_banner is not None:
                        self.quota_banner.set_revealed(False)
                    if mode == "alarm":
                        self._render_all(
                            f"Cleanup freed {result.bytes_freed} bytes "
                            f"({result.chunks_freed} chunks). Uploads "
                            f"resumed — try {action.lower()} again.",
                            "success",
                        )
                    else:
                        self._render_all(
                            f"Reclaimed {result.bytes_freed} bytes "
                            f"({result.chunks_freed} chunks). "
                            f"Try {action.lower()} again.",
                            "success",
                        )
                return False
            GLib.idle_add(succeed)

        threading.Thread(target=worker, daemon=True).start()
