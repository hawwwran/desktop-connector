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

from src.vault.binding.preflight import (  # noqa: E402
    PER_OP_FLOOR_S,
    PER_OP_GROWTH_S_PER_ENTRY,
    WARNING_THRESHOLD_S,
    compute_preflight,
    count_manifest_entries,
    estimate_drain_seconds,
    format_duration,
    render_preflight_text,
)
from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    make_folder_shard,
    make_root_folder_pointer,
    make_root_manifest,
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
    root = make_root_manifest(
        vault_id=VAULT_ID,
        root_revision=2, parent_root_revision=1,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_root_folder_pointer(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
            )
        ],
    )
    shard = make_folder_shard(
        vault_id=VAULT_ID,
        remote_folder_id=DOCS_ID,
        shard_revision=2, parent_shard_revision=1,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        entries=entries,
    )
    return assemble_unified_manifest(root, {DOCS_ID: shard})


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

    def test_symlinks_are_not_counted_as_files(self) -> None:
        """Review §3.C3: a symlink dropped into the binding root must
        not show up in the preflight counts. Pre-fix, ``path.stat()``
        followed the link and we counted the target's size, inflating
        the bind plan and previewing material we should never read.
        """
        target = self.tmpdir / "Documents"
        target.mkdir(parents=True)
        (target / "real.txt").write_bytes(b"x" * 64)
        # Create a symlink that resolves to a regular file outside the
        # binding root — the security-relevant scenario.
        outside = self.tmpdir / "outside"
        outside.write_bytes(b"y" * 1024)
        os.symlink(outside, target / "link.txt")

        manifest = _manifest_with([])
        summary = compute_preflight(
            manifest=manifest, remote_folder_id=DOCS_ID, local_root=target,
        )
        # Only the real file counts — the symlink is filtered.
        self.assertEqual(summary.local_existing_files, 1)
        self.assertEqual(summary.local_existing_bytes, 64)


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


class BindDurationEstimatorTests(unittest.TestCase):
    """Phase 1 of ``temp/finished-plans/vault-large-folder-perf.md``.

    The estimator is calibrated against suite 0004 / 2026-05-16 data
    (the B7 step-ladder run). The tests anchor:
    - shape of the integral (linear in N for small N; quadratic in N
      for large N when start_manifest_entries is small);
    - the warning threshold matches the published 2-minute decision
      point;
    - degenerate inputs (zero uploads, negative start) don't blow up.
    """

    def test_zero_uploads_is_instant(self) -> None:
        self.assertEqual(
            estimate_drain_seconds(start_manifest_entries=0, new_uploads=0),
            0.0,
        )
        self.assertEqual(
            estimate_drain_seconds(
                start_manifest_entries=10_000, new_uploads=0,
            ),
            0.0,
        )

    def test_negative_start_clamped(self) -> None:
        # Defensive: a bad manifest count shouldn't surface as a
        # negative duration (which would print "less than a second"
        # and silently bypass the warning).
        out = estimate_drain_seconds(
            start_manifest_entries=-50, new_uploads=100,
        )
        self.assertGreater(out, 0.0)

    def test_100_files_from_empty_below_warning(self) -> None:
        # Matches the post-SO-2+SO-3 100-file run (~1.7 s observed).
        # Estimator stays well below the warning threshold.
        seconds = estimate_drain_seconds(
            start_manifest_entries=0, new_uploads=100,
        )
        self.assertGreater(seconds, 0.0)
        self.assertLess(seconds, 30.0)
        self.assertLess(seconds, WARNING_THRESHOLD_S)

    def test_1k_files_from_empty_matches_post_so23_within_50_percent(self) -> None:
        # Post-SO-2+SO-3 1k bind drain: 15.6 s for 1 000 ops from
        # empty (2026-05-16 live re-test). Estimator's job is to land
        # in the same ballpark; conservative side is fine.
        seconds = estimate_drain_seconds(
            start_manifest_entries=0, new_uploads=1000,
        )
        self.assertGreaterEqual(seconds, 15.6 * 0.5)
        self.assertLessEqual(seconds, 15.6 * 2.0)
        # Still below the warning threshold — 1 000 files isn't
        # "painful" anymore post-Phase-2.
        self.assertLess(seconds, WARNING_THRESHOLD_S)

    def test_3000_files_from_empty_above_warning(self) -> None:
        # ~3 000 files from an empty vault is the post-Phase-2 trigger
        # point for the 2-minute warning (the dialog still fires for
        # the genuinely-slow case; we just don't warn on what's now
        # a fast bind). Pins the threshold so a future refit doesn't
        # silently slide it below 1 000 files (where users would see
        # the dialog for a 20-second op — annoying).
        seconds = estimate_drain_seconds(
            start_manifest_entries=0, new_uploads=3000,
        )
        self.assertGreaterEqual(seconds, WARNING_THRESHOLD_S)
        # And from 1 000, we explicitly stay BELOW threshold.
        below = estimate_drain_seconds(
            start_manifest_entries=0, new_uploads=1000,
        )
        self.assertLess(below, WARNING_THRESHOLD_S)

    def test_10k_files_matches_post_so23_within_50_percent(self) -> None:
        # Post-SO-2+SO-3 10k bind drain: 1 216 s (~20 min) for 10 000
        # uploads from empty (2026-05-16 live re-test).
        seconds = estimate_drain_seconds(
            start_manifest_entries=0, new_uploads=10000,
        )
        # Lower bound: must not under-warn beyond the 50 % tolerance.
        # (Multi-device contention or a slow relay can make reality
        # exceed the estimate.)
        self.assertGreaterEqual(seconds, 1216 * 0.5)
        # Upper bound: cap conservatism so the dialog doesn't quote
        # 2 hours for what is now a 20-minute op.
        self.assertLessEqual(seconds, 1216 * 2.0)

    def test_formula_constants_match_documented_fit(self) -> None:
        # Constants live in source; tests pin the values so they don't
        # drift without an explicit doc update.
        self.assertEqual(PER_OP_FLOOR_S, 0.005)
        self.assertEqual(PER_OP_GROWTH_S_PER_ENTRY, 0.000025)
        self.assertEqual(WARNING_THRESHOLD_S, 120.0)

    def test_format_duration_thresholds(self) -> None:
        self.assertEqual(format_duration(0.0), "less than a second")
        self.assertIn("second", format_duration(5.0))
        self.assertIn("minute", format_duration(65.0))
        self.assertIn("hours", format_duration(8000.0))


class ManifestEntryCountTests(unittest.TestCase):
    """``count_manifest_entries`` sums versions across folders.

    The estimator needs the *total* manifest size, not just one
    folder's entries, because each upload publishes the full
    manifest envelope.
    """

    def test_empty_manifest_is_zero(self) -> None:
        self.assertEqual(count_manifest_entries({}), 0)
        self.assertEqual(count_manifest_entries({"remote_folders": []}), 0)

    def test_counts_versions_not_entries(self) -> None:
        manifest = _manifest_with([
            _entry("a.txt", version_id="fv_v1_a" * 5, size=1),
            _entry("b.txt", version_id="fv_v1_b" * 5, size=2),
        ])
        # add an extra version onto entry "a.txt"
        for f in manifest["remote_folders"]:
            for e in f.get("entries", []):
                if e["path"] == "a.txt":
                    e["versions"].append({
                        "version_id": "fv_v1_a2" * 3,
                        "created_at": "2026-05-01T11:00:00.000Z",
                        "modified_at": "2026-05-01T11:00:00.000Z",
                        "logical_size": 1,
                        "ciphertext_size": 33,
                        "content_fingerprint": "abc",
                        "chunks": [],
                        "author_device_id": AUTHOR,
                    })
        # 2 entries × 1 version + 1 extra version = 3
        self.assertEqual(count_manifest_entries(manifest), 3)

    def test_summary_carries_estimate_and_threshold_flag(self) -> None:
        """Source-pin: compute_preflight returns the perf estimate alongside
        the §D15 counts so the connect dialog can gate on a single value.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="vault_estimate_test_"))
        try:
            target = tmpdir / "many"
            target.mkdir()
            for i in range(5):
                (target / f"f{i}.txt").write_bytes(b"x")
            summary = compute_preflight(
                manifest=_manifest_with([]),
                remote_folder_id=DOCS_ID,
                local_root=target,
            )
            # 5 files from empty manifest: estimator returns a small
            # positive duration, threshold not hit.
            self.assertEqual(summary.local_existing_files, 5)
            self.assertEqual(summary.starting_manifest_entries, 0)
            self.assertGreater(summary.projected_upload_drain_seconds, 0.0)
            self.assertFalse(summary.bind_warning_threshold_hit)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class SlowBindDialogSourceTests(unittest.TestCase):
    """Source-pin: the slow-bind warning dialog is wired correctly.

    We can't drive an Adw.MessageDialog in a headless test, but we
    can verify the dialog helper exists, calls Adw.MessageDialog,
    routes the Cancel response to no-op (no binding row written),
    and routes Start sync to the existing create-binding worker.
    """

    def test_connect_dialog_imports_warning_helpers(self) -> None:
        from _paths import REPO_ROOT
        source = Path(
            REPO_ROOT, "desktop/src/vault/folder/connect_dialog.py"
        ).read_text(encoding="utf-8")
        for needle in (
            "format_duration",
            "_present_slow_bind_confirm",
            "_UPLOADING_MODES",
            "Adw.MessageDialog",
            "Large folder",
            "Start sync",
            "bind_warning_threshold_hit",
        ):
            with self.subTest(text=needle):
                self.assertIn(needle, source)

    def test_cancel_path_does_not_call_create_binding(self) -> None:
        # Pin: only the "start" response triggers the binding create
        # worker; cancel returns silently (the existing dialog stays
        # open so the user can pick a different sync mode / folder).
        from _paths import REPO_ROOT
        source = Path(
            REPO_ROOT, "desktop/src/vault/folder/connect_dialog.py"
        ).read_text(encoding="utf-8")
        # The slow-bind helper routes only the "start" response to
        # on_confirm — `_start_create_worker` must be the on_confirm
        # callback inside on_connect.
        self.assertIn(
            'if response == "start":', source,
        )
        self.assertIn(
            "on_confirm=lambda: _start_create_worker(remote, mode)",
            source,
        )


class ConnectFolderUiSourceTests(unittest.TestCase):
    """T10.2 source-pin: dialog + folders-tab wiring."""

    def test_folders_tab_offers_connect_local_folder_button(self) -> None:
        # F-LT09 redesign moved the connect entry-point from a global
        # Folders-tab button to a per-folder card action that's only
        # rendered when the selected folder has zero bindings (so users
        # can't double-bind the same folder by accident). The pin
        # tracks the new shape: per-folder ``connect_btn`` labelled
        # "Connect with local folder", invoked through
        # ``open_connect_local_dialog`` with the ``rfid`` already
        # known.
        from _paths import REPO_ROOT
        # Post-#6: the tab is a ``vault_folders/`` package; concatenate
        # the submodules so the pin sees all the strings regardless of
        # which submodule wires the Connect button vs. the dialog.
        package_dir = Path(REPO_ROOT, "desktop/src/vault_folders")
        source = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted(package_dir.glob("*.py"))
        )
        for needle in (
            # Package siblings reach up via ``..`` rather than ``.``.
            "from ..vault.folder.connect_dialog import present_connect_folder_dialog",
            "from ..vault.binding.bindings import VaultBindingsStore",
            "connect_btn = Gtk.Button(",
            'label="Connect with local folder"',
            "open_connect_local_dialog",
            "needs-preflight",
        ):
            with self.subTest(text=needle):
                self.assertIn(needle, source)

    def test_dialog_module_default_mode_is_backup_only(self) -> None:
        from _paths import REPO_ROOT
        source = Path(REPO_ROOT, "desktop/src/vault/folder/connect_dialog.py").read_text(
            encoding="utf-8"
        )
        for needle in (
            'from ..binding.preflight import (',
            "compute_preflight",
            "render_preflight_text",
            "DEFAULT_MODE_INDEX",
            # Title copy aligned with the per-folder button.
            "Connect with local folder",
            'state="needs-preflight"',
            'on_cancel',
        ):
            with self.subTest(text=needle):
                self.assertIn(needle, source)


if __name__ == "__main__":
    unittest.main()
