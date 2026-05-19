"""SyncStatusBannerMixin — ambient "Vault sync K/N" indicator.

Polls the local-index ``vault_pending_operations`` table every
``POLL_INTERVAL_MS`` and renders a compact banner at the top of the
Vault Browser when ≥ 1 op is pending. The widget hides itself when
the queue is empty.

K/N tracking: ``session_max`` is the high-water mark since the last
time the queue reached zero. As ops drain, the banner reads
``"Vault sync (session_max − pending)/session_max"``. If the user
drops more files mid-sync, ``session_max`` climbs to the new max
(never decreases until a clean 0).

The widget is constructed by ``LayoutMixin._build_breadcrumb_and_status``
alongside the resume banner; this mixin owns the GLib polling + label
refresh.

Spec: ``temp/finished-plans/vault-large-folder-perf.md`` Phase 1.5 /
SO-3 from ``temp/finished-plans/live-testing-followup.partly.md`` §13.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from ..vault.binding.bindings import VaultBindingsStore

log = logging.getLogger(__name__)


# 1.5 s is fast enough to feel live but cheap on SQLite — at 10k pending
# rows the COUNT(*) is sub-millisecond on a WAL-mode DB.
POLL_INTERVAL_MS = 1500


class SyncStatusBannerMixin:
    """Ambient pending-ops counter at the top of the Vault Browser."""

    def _start_sync_status_polling(self) -> None:
        """Kick off the GLib timeout that keeps the banner fresh.

        Idempotent: a second call no-ops if the source is already
        scheduled. The poll function returns ``True`` to stay armed;
        we keep no separate stop handle because the timeout source
        dies when the window's main loop tears down.
        """
        if getattr(self, "_sync_status_poll_source", None) is not None:
            return
        # Track high-water mark for the current "drain session". Resets
        # to 0 when the queue empties so the next burst starts fresh.
        self._sync_status_session_max = 0
        # One tick immediately so the banner reflects current state at
        # window open rather than waiting POLL_INTERVAL_MS.
        self._refresh_sync_status_banner()
        self._sync_status_poll_source = GLib.timeout_add(
            POLL_INTERVAL_MS, self._on_sync_status_tick,
        )

    def _on_sync_status_tick(self) -> bool:
        try:
            self._refresh_sync_status_banner()
        except Exception:  # noqa: BLE001
            log.exception("vault.browser.sync_status_tick_failed")
        return True  # keep ticking

    def _refresh_sync_status_banner(self) -> None:
        box = getattr(self, "sync_status_banner_box", None)
        label = getattr(self, "sync_status_banner_label", None)
        if box is None or label is None:
            return

        vault_id = self._resolve_vault_id()
        if not vault_id:
            box.set_visible(False)
            return

        try:
            store = VaultBindingsStore(self.local_index.db_path)
            pending = store.count_pending_ops_for_vault(vault_id)
        except Exception:  # noqa: BLE001
            log.exception("vault.browser.sync_status_count_failed")
            box.set_visible(False)
            return

        if pending <= 0:
            self._sync_status_session_max = 0
            box.set_visible(False)
            return

        # Bump the session high-water mark if the queue just grew.
        if pending > self._sync_status_session_max:
            self._sync_status_session_max = pending
        done = max(0, self._sync_status_session_max - pending)
        total = self._sync_status_session_max
        label.set_label(f"Vault sync {done:,}/{total:,}")
        box.set_visible(True)
