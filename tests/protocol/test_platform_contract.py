"""Contract tests for the desktop platform boundary (refactor-10).

These tests pin the shape of ``DesktopPlatform`` / ``PlatformCapabilities``
and the behavior of ``compose_desktop_platform`` on Linux and non-Linux hosts.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.platform import DesktopPlatform, PlatformCapabilities  # noqa: E402
from src.platform.compose import compose_desktop_platform  # noqa: E402
from src.platform.linux.compose import compose_linux_platform  # noqa: E402


class LinuxPlatformContractTests(unittest.TestCase):
    def test_linux_platform_is_desktop_platform(self):
        p = compose_linux_platform()
        self.assertIsInstance(p, DesktopPlatform)
        self.assertEqual(p.name, "linux")

    def test_linux_platform_wires_all_four_backends(self):
        p = compose_linux_platform()
        # All contracts are populated; methods exist.
        self.assertTrue(hasattr(p.clipboard, "write_text"))
        self.assertTrue(hasattr(p.notifications, "notify"))
        self.assertTrue(hasattr(p.dialogs, "pick_files"))
        self.assertTrue(hasattr(p.shell, "open_path"))
        self.assertTrue(hasattr(p.shell, "launch_installer_terminal"))

    def test_linux_capabilities_match_expected_defaults(self):
        p = compose_linux_platform()
        self.assertIsInstance(p.capabilities, PlatformCapabilities)
        # Linux supports every capability we define today.
        self.assertTrue(p.capabilities.clipboard_text)
        self.assertTrue(p.capabilities.clipboard_image)
        self.assertTrue(p.capabilities.notifications)
        self.assertTrue(p.capabilities.tray)
        self.assertTrue(p.capabilities.file_manager_integration)
        self.assertTrue(p.capabilities.auto_open_urls)
        self.assertTrue(p.capabilities.open_folder)
        self.assertTrue(p.capabilities.installer_terminal)


class ComposeDesktopPlatformTests(unittest.TestCase):
    def test_linux_host_returns_linux_platform(self):
        with mock.patch.object(sys, "platform", "linux"):
            p = compose_desktop_platform()
        self.assertEqual(p.name, "linux")

    def test_non_linux_host_raises_not_implemented(self):
        # Silent fallback to Linux on Windows/macOS caused confusing runtime
        # failures deep in the stack; this pins the explicit error.
        with mock.patch.object(sys, "platform", "win32"):
            with self.assertRaises(NotImplementedError) as ctx:
                compose_desktop_platform()
        self.assertIn("win32", str(ctx.exception))

        with mock.patch.object(sys, "platform", "darwin"):
            with self.assertRaises(NotImplementedError):
                compose_desktop_platform()


class ContractImportDecouplingTests(unittest.TestCase):
    def test_contract_import_does_not_load_linux_impl(self):
        # Regression: importing the contract alone must not pull in any
        # Linux-specific backend module, so non-Linux runtimes can at least
        # type-check/core-import without dragging in wl-copy/xclip wrappers.
        loaded_before = {m for m in sys.modules if "backends.linux" in m}

        import importlib

        importlib.import_module("src.platform.contract")

        loaded_after = {m for m in sys.modules if "backends.linux" in m}
        newly_loaded = loaded_after - loaded_before
        self.assertEqual(
            newly_loaded,
            set(),
            f"src.platform.contract import pulled in Linux backends: {newly_loaded!r}",
        )


if __name__ == "__main__":
    unittest.main()
