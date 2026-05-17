"""Review §6.C1 / §6.C2 / §6.C3 — long-running operations on the GTK
main thread.

These three findings all share the same regression risk: an Argon2id
verify or a relay POST embedded directly in a GTK click handler
freezes the main loop for 1-10s and reads as "the app crashed",
driving users to force-quit and lose fresh recovery material or
abandon a retry that was about to succeed.

The fixes off-load the work to ``threading.Thread`` workers that
settle back to the main thread via ``GLib.idle_add``. We can't drive
the actual subprocess windows from a unit test without a full AT-SPI
harness — so this file does a source-level smoke check that the
relevant click handlers contain a worker, and that no inline
``verify_recovery_kit(...)`` / ``publish_initial(relay)`` /
``run_recovery_material_test(...)`` call survives outside a worker.

If a future refactor re-inlines the call, this test fails and the
review §6.C1-C3 regression is caught before the user sees a hung
window.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

TAB_RECOVERY = (
    Path(REPO_ROOT)
    / "desktop"
    / "src"
    / "windows_vault"
    / "tab_recovery.py"
)
ONBOARD_WINDOW = (
    Path(REPO_ROOT)
    / "desktop"
    / "src"
    / "windows_vault"
    / "onboard_window.py"
)
BROWSER_DELETE = (
    Path(REPO_ROOT)
    / "desktop"
    / "src"
    / "windows_vault_browser"
    / "delete_restore.py"
)
TAB_DANGER = (
    Path(REPO_ROOT)
    / "desktop"
    / "src"
    / "windows_vault"
    / "tab_danger.py"
)


class UiOffloadSmokeTests(unittest.TestCase):
    def test_tab_recovery_on_test_uses_worker_thread(self) -> None:
        """§6.C1: ``on_test`` must run ``run_recovery_material_test``
        inside a threading.Thread worker, not inline on the GTK main
        thread."""
        source = TAB_RECOVERY.read_text()
        on_test_body = _slice_function(source, "def on_test(")
        self.assertIn(
            "threading.Thread", on_test_body,
            "tab_recovery.on_test must off-load Argon2id to a worker",
        )
        self.assertIn(
            "GLib.idle_add", on_test_body,
            "tab_recovery.on_test must settle on the main thread via idle_add",
        )
        # No inline call to the blocking primitive at the click-handler
        # level: every reference is inside the worker.
        inline_calls = _inline_calls_outside_worker(on_test_body, "run_recovery_material_test")
        self.assertEqual(
            inline_calls, [],
            f"run_recovery_material_test must not be invoked inline; "
            f"saw: {inline_calls}",
        )

    def test_onboard_export_verify_uses_worker_thread(self) -> None:
        """§6.C2: the file-dialog callback that does the post-export
        kit verify must run ``verify_recovery_kit`` inside a worker."""
        source = ONBOARD_WINDOW.read_text()
        callback_body = _slice_function(source, "def on_file_chosen(")
        self.assertIn(
            "threading.Thread", callback_body,
            "on_file_chosen must off-load verify_recovery_kit to a worker",
        )
        self.assertIn(
            "GLib.idle_add", callback_body,
            "on_file_chosen must settle on the main thread via idle_add",
        )
        inline_calls = _inline_calls_outside_worker(
            callback_body, "verify_recovery_kit",
        )
        self.assertEqual(
            inline_calls, [],
            f"verify_recovery_kit must not be invoked inline; saw: {inline_calls}",
        )

    def test_browser_delete_folder_routes_through_fresh_unlock_and_typed_confirm(self) -> None:
        """§6.C4: Vault Browser per-row "Delete folder contents" must
        run through ``require_fresh_unlock_or_prompt`` AND open an
        AlertDialog with a typed-confirm Entry. Pre-fix the hamburger
        skipped both — quiet bypass of the Danger tab's gates."""
        source = BROWSER_DELETE.read_text()
        handler_body = _slice_function(source, "def _confirm_delete_folder(")
        self.assertIn(
            "require_fresh_unlock_or_prompt", handler_body,
            "_confirm_delete_folder must gate on fresh-unlock",
        )
        # Confirm-text helper invoked in the confirm-dialog branch.
        confirm_body = _slice_function(
            source, "def _open_delete_folder_confirm_dialog(",
        )
        self.assertIn(
            "confirm_folder_clear_text_matches", confirm_body,
            "the post-fresh-unlock dialog must use the typed-confirm helper",
        )
        self.assertIn(
            "Gtk.Entry", confirm_body,
            "the confirm dialog must include a Gtk.Entry for typed-confirm",
        )
        # The "delete" response must start disabled until the user
        # types the folder name correctly.
        self.assertIn(
            'set_response_enabled("delete", False)', confirm_body,
            "the Delete response must start disabled (typed-confirm gate)",
        )

    def test_schedule_purge_checks_admin_role(self) -> None:
        """§6.C5: ``on_schedule_purge`` must check the device's role
        via the relay before opening the schedule dialog. Pre-fix the
        button only gated on typed-confirm + fresh-unlock — anyone
        with the recovery passphrase could schedule a destructive
        purge regardless of role."""
        source = TAB_DANGER.read_text()
        handler_body = _slice_function(source, "def on_schedule_purge(")
        # Fresh-unlock first.
        self.assertIn(
            "require_fresh_unlock_or_prompt", handler_body,
            "on_schedule_purge must still gate on fresh-unlock",
        )
        # Then a worker that consults caller_role from the relay's
        # GET /header response.
        self.assertIn(
            "threading.Thread", handler_body,
            "on_schedule_purge must check role on a worker (avoid blocking the GTK main loop)",
        )
        self.assertIn(
            "caller_role", handler_body,
            "on_schedule_purge must read caller_role from the relay response",
        )
        # The dialog opens only when role == admin.
        self.assertIn(
            "!= \"admin\"", handler_body,
            "on_schedule_purge must compare role against 'admin' before opening the dialog",
        )

    def test_onboard_retry_publish_uses_worker_thread(self) -> None:
        """§6.C3: the "Retry publish" handler must run the relay POST
        inside a worker so a flaky relay doesn't freeze the wizard."""
        source = ONBOARD_WINDOW.read_text()
        handler_body = _slice_function(source, "def on_retry_publish(")
        self.assertIn(
            "threading.Thread", handler_body,
            "on_retry_publish must off-load publish_initial to a worker",
        )
        self.assertIn(
            "GLib.idle_add", handler_body,
            "on_retry_publish must settle on the main thread via idle_add",
        )
        inline_calls = _inline_calls_outside_worker(
            handler_body, "publish_initial",
        )
        self.assertEqual(
            inline_calls, [],
            f"vault.publish_initial(relay) must not be invoked inline; "
            f"saw: {inline_calls}",
        )


def _slice_function(source: str, header: str) -> str:
    """Return the body of a function whose def-line starts with ``header``.

    Supports multi-line def headers (parameters wrapped across lines):
    skip ahead until we find the colon-terminator that introduces the
    function body, then slice from there.
    """
    idx = source.find(header)
    if idx == -1:
        raise AssertionError(f"function not found: {header}")
    line_start = source.rfind("\n", 0, idx) + 1
    base_indent = idx - line_start
    # Find the colon that closes the def signature. For multi-line
    # headers Python requires the close-paren+":"+newline pattern at
    # the same outer indent as the def keyword; walk forward
    # paren-balanced and stop at the first ":" at depth 0.
    depth = 0
    cursor = idx + len(header) - 1   # position of the opening paren
    while cursor < len(source):
        ch = source[cursor]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ":" and depth == 0:
            break
        cursor += 1
    body_start = source.index("\n", cursor) + 1
    body_lines: list[str] = []
    cursor = body_start
    while cursor < len(source):
        eol = source.find("\n", cursor)
        if eol == -1:
            eol = len(source)
        line = source[cursor:eol]
        stripped = line.strip()
        if stripped:
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent:
                break
        body_lines.append(line)
        cursor = eol + 1
    return "\n".join(body_lines)


def _inline_calls_outside_worker(body: str, call_name: str) -> list[str]:
    """Scan a function body for ``call_name(`` invocations that live
    outside any nested ``def worker``-like inner function. The exact
    rule: drop every line below an inner ``def <name>(`` header at
    indent > the body's own base, until the indent returns. What's
    left is the "outer" body — any ``call_name(`` there is an inline
    call on the GTK main thread."""
    lines = body.splitlines()
    base_indent: int | None = None
    for line in lines:
        if line.strip():
            base_indent = len(line) - len(line.lstrip())
            break
    if base_indent is None:
        return []
    outer_lines: list[str] = []
    inside_inner = False
    inner_indent: int | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not inside_inner:
                outer_lines.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if inside_inner:
            if indent <= inner_indent:
                inside_inner = False
                inner_indent = None
            else:
                continue
        if not inside_inner and re.match(r"def\s+\w+\(", stripped):
            inside_inner = True
            inner_indent = indent
            continue
        if not inside_inner:
            outer_lines.append(line)
    outer = "\n".join(outer_lines)
    return [
        m.group(0)
        for m in re.finditer(rf"\b{re.escape(call_name)}\s*\(", outer)
    ]


if __name__ == "__main__":
    unittest.main()
