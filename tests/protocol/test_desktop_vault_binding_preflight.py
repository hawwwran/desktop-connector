"""T10.2 — Connect-folder preflight summary."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_binding_preflight import (  # noqa: E402
    compute_preflight,
    render_preflight_text,
)
from src.vault_manifest import (  # noqa: E402
    make_manifest,
    make_remote_folder,
)


VAULT_ID = "ABCD2345WXYZ"
DOCS_ID = "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa"
AUTHOR = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def _entry(path: str, *, version_id: str, size: int, deleted: bool = False, recoverable_until: str = "") -> dict:
    entry: dict = {
        "entry_id": "fe_v1_" + (path.replace("/", "_").ljust(24, "x"))[:24],
        "type": "file",
        "path": path,
        "deleted": deleted,
        "latest_version_id": version_id,
        "versions": [{
            "version_id": version_id,
            "created_at": "2026-05-01T10:00:00.000Z",
            "modified_at": "2026-05-01T10:00:00.000Z",
            "logical_size": size,
            "ciphertext_size": size + 32,
            "content_fingerprint": "abc",
            "chunks": [],
            "author_device_id": AUTHOR,
        }],
    }
    if deleted:
        entry["deleted_at"] = "2026-05-01T10:00:00.000Z"
        entry["deleted_by_device_id"] = AUTHOR
        if recoverable_until:
            entry["recoverable_until"] = recoverable_until
    return entry


def _manifest_with(entries: list[dict]) -> dict:
    return make_manifest(
        vault_id=VAULT_ID,
        revision=2, parent_revision=1,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=entries,
            )
        ],
    )


class PreflightCountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_preflight_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_counts_current_and_deleted_separately(self) -> None:
        """T10.2 acceptance: preflight numbers add up."""
        manifest = _manifest_with([
            _entry("a.txt", version_id="fv_v1_a" * 5, size=1024),
            _entry("nested/b.txt", version_id="fv_v1_b" * 5, size=2048),
            _entry("ghost.txt", version_id="fv_v1_c" * 5, size=512,
                   deleted=True, recoverable_until="2026-06-01T00:00:00.000Z"),
            _entry("older-ghost.txt", version_id="fv_v1_d" * 5, size=128,
                   deleted=True, recoverable_until="2026-05-15T00:00:00.000Z"),
        ])

        summary = compute_preflight(
            manifest=manifest,
            remote_folder_id=DOCS_ID,
            local_root=self.tmpdir / "fresh-folder",  # doesn't exist yet
        )

        self.assertEqual(summary.current_files, 2)
        self.assertEqual(summary.current_bytes, 1024 + 2048)
        self.assertEqual(summary.deleted_files, 2)
        self.assertEqual(summary.deleted_bytes, 512 + 128)
        # Earliest recoverable: alphabetic min on RFC3339 strings = the
        # 2026-05-15 tombstone (sooner expiry).
        self.assertEqual(
            summary.earliest_recoverable_until,
            "2026-05-15T00:00:00.000Z",
        )
        self.assertFalse(summary.local_path_exists)
        # Local existing counts must be zero when the folder doesn't exist.
        self.assertEqual(summary.local_existing_files, 0)
        self.assertEqual(summary.local_existing_bytes, 0)

    def test_local_existing_files_counted_when_path_exists(self) -> None:
        target = self.tmpdir / "Documents"
        target.mkdir(parents=True)
        (target / "extra1.txt").write_bytes(b"already here")
        (target / "extra2.txt").write_bytes(b"also already here")
        # Hidden dotfile is ignored by default.
        (target / ".hidden").write_bytes(b"ignored")

        manifest = _manifest_with([
            _entry("only.txt", version_id="fv_v1_a" * 5, size=512),
        ])
        summary = compute_preflight(
            manifest=manifest, remote_folder_id=DOCS_ID, local_root=target,
        )
        self.assertEqual(summary.local_existing_files, 2)
        self.assertGreater(summary.local_existing_bytes, 0)
        self.assertTrue(summary.local_path_exists)

    def test_unknown_remote_folder_yields_zero_counts(self) -> None:
        manifest = _manifest_with([])
        summary = compute_preflight(
            manifest=manifest,
            remote_folder_id="rf_v1_z" * 5,
            local_root=self.tmpdir,
        )
        self.assertEqual(summary.remote_folder_display_name, "")
        self.assertEqual(summary.current_files, 0)
        self.assertEqual(summary.deleted_files, 0)


class PreflightTextTests(unittest.TestCase):
    def test_renders_d15_layout_with_tombstone_line(self) -> None:
        """§D15 wording: tombstones get their own informational line."""
        summary = compute_preflight(
            manifest=_manifest_with([
                _entry("a.txt", version_id="fv_v1_a" * 5, size=4 * 1024 * 1024 * 1024 + 100_000_000),
                _entry("ghost.txt", version_id="fv_v1_b" * 5, size=128,
                       deleted=True, recoverable_until="2026-06-01T00:00:00.000Z"),
            ]),
            remote_folder_id=DOCS_ID,
            local_root=Path("/tmp/does-not-exist"),
        )
        text = render_preflight_text(summary)
        self.assertIn('Remote folder "Documents"', text)
        self.assertIn("1 deleted files", text)
        self.assertIn(
            "Deleted files will not be applied to your local folder "
            "during initial binding.",
            text,
        )
        self.assertIn("recoverable until 2026-06-01", text)

    def test_warns_when_local_parent_unwritable(self) -> None:
        # Use /proc which exists but isn't writable — a portable test
        # that doesn't depend on any path setup.
        summary = compute_preflight(
            manifest=_manifest_with([]),
            remote_folder_id=DOCS_ID,
            local_root=Path("/proc/sys/kernel/dc_vault_test_target"),
        )
        text = render_preflight_text(summary)
        self.assertIn("Warning", text)


class ConnectFolderUiSourceTests(unittest.TestCase):
    """T10.2 source-pin: dialog + folders-tab wiring."""

    def test_folders_tab_offers_connect_local_folder_button(self) -> None:
        from _paths import REPO_ROOT
        source = Path(REPO_ROOT, "desktop/src/vault_folders_tab.py").read_text(
            encoding="utf-8"
        )
        for needle in (
            "from .vault_connect_folder_dialog import present_connect_folder_dialog",
            "from .vault_bindings import VaultBindingsStore",
            'connect_local_btn = Gtk.Button(label="Connect local folder…"',
            "open_connect_local_dialog",
            "needs-preflight",
        ):
            with self.subTest(text=needle):
                self.assertIn(needle, source)

    def test_dialog_module_default_mode_is_backup_only(self) -> None:
        from _paths import REPO_ROOT
        source = Path(REPO_ROOT, "desktop/src/vault_connect_folder_dialog.py").read_text(
            encoding="utf-8"
        )
        for needle in (
            'from .vault_binding_preflight import (',
            "compute_preflight",
            "render_preflight_text",
            "DEFAULT_MODE_INDEX",
            "Connect local folder",
            'state="needs-preflight"',
            'on_cancel',
        ):
            with self.subTest(text=needle):
                self.assertIn(needle, source)


if __name__ == "__main__":
    unittest.main()
