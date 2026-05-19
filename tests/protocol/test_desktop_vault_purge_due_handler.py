"""Suite 0007 B1 — behavioural test for ``_handle_due_purges_for_tick``.

The live B1 test caught ``vault_submenu.py:527`` reading
``pending.vault_id_dashed`` on a :class:`PendingPurge`, which has no
such field — the dataclass holds the dashed form under
``vault_id``. The wrapper ``try/except`` in the autosync loop caught
the AttributeError as ``vault.sync.autosync_purge_check_failed``, so
the loop survived but **the user-facing notification never fired**.
The §6.H1 wiring was silently dead.

Source-string presence tests (the older ones in
``test_desktop_vault_ui_offload.py``) didn't catch this because the
right strings WERE in the source — they just referenced a
non-existent attribute. This test runs the handler against a fake
``self`` and a real :class:`PendingPurge` so a future regression on
the same field-name mismatch fails loudly.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.tray.vault_submenu import VaultSubmenuMixin  # noqa: E402
from src.vault.ops.purge_schedule import (  # noqa: E402
    MIN_DELAY_SECONDS, schedule_purge,
)


VAULT_DASHED = "X2Z3-EBY3-SKVN"


class _FakeNotifications:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, body: str, icon: str = "dialog-information") -> None:
        self.calls.append((title, body))


class _FakePlatform:
    def __init__(self) -> None:
        self.notifications = _FakeNotifications()


class _FakeConfig:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir


class _FakeMixinHost(VaultSubmenuMixin):
    """Minimal host so ``_handle_due_purges_for_tick`` can run."""

    def __init__(self, config_dir: Path) -> None:
        self.config = _FakeConfig(config_dir)
        self.platform = _FakePlatform()


class HandleDuePurgesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_b1_purge_"))
        self.config_dir = self.tmpdir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _schedule_due_purge(self) -> None:
        """Persist a real purge record and backdate it so it's due."""
        schedule_purge(
            self.config_dir,
            vault_id_dashed=VAULT_DASHED,
            scope="vault",
            scope_target=None,
            scheduled_by_device_id="d" * 32,
            delay_seconds=MIN_DELAY_SECONDS,
        )
        # Backdate by 1 hour so list_due_purges sees it as due.
        p = self.config_dir / "vault_pending_purges.json"
        state = json.loads(p.read_text())
        for rec in state.values():
            rec["scheduled_for_epoch"] = int(time.time()) - 3600
        p.write_text(json.dumps(state, sort_keys=True))

    def test_due_purge_fires_user_notification(self) -> None:
        """The handler must surface a system notification for every
        newly-due purge.

        Suite 0007 B1 regression guard: the handler used to read
        ``pending.vault_id_dashed``, which AttributeError'd because the
        dataclass field is ``vault_id``. The exception was caught by
        the autosync wrapper, silently disabling §6.H1. If a future
        regression on the same dataclass-attribute mismatch lands, this
        test raises AttributeError synchronously instead of getting
        swallowed by the production try/except.
        """
        self._schedule_due_purge()
        host = _FakeMixinHost(self.config_dir)

        # First tick: notification fires.
        host._handle_due_purges_for_tick()
        self.assertEqual(
            len(host.platform.notifications.calls), 1,
            "due purge must surface exactly one notification",
        )
        title, body = host.platform.notifications.calls[0]
        self.assertIn("Hard purge is due", title)
        self.assertIn("Danger zone", body)

    def test_due_purge_notification_is_idempotent_within_process(self) -> None:
        """Second tick on the same due purge must not re-notify — the
        handler tracks ``_vault_purge_notified`` keyed by
        ``(vault_id, job_id)`` to suppress duplicates."""
        self._schedule_due_purge()
        host = _FakeMixinHost(self.config_dir)

        host._handle_due_purges_for_tick()
        host._handle_due_purges_for_tick()
        self.assertEqual(
            len(host.platform.notifications.calls), 1,
            "second tick on the same due purge must not re-notify",
        )

    def test_no_due_purge_fires_no_notification(self) -> None:
        """No state file = no-op. (Also covers the "purge already
        cleared by the user since last tick" case.)"""
        host = _FakeMixinHost(self.config_dir)
        host._handle_due_purges_for_tick()
        self.assertEqual(host.platform.notifications.calls, [])


if __name__ == "__main__":
    unittest.main()
