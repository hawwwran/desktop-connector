"""Suite 0007 B2 fix — ``collect_bundle_inputs`` threads all 5 inputs.

The producer-side gap caught in the live B2 test was that
``tab_maintenance``'s worker only passed 3 of the 5 inputs the bundle
builder accepts: ``binding_states`` and ``manifest_summary`` were
silently omitted, even though the UI label promises "binding states".

The fix extracted the input collection into a pure helper that the
worker calls. These tests pin:

- All 5 keys are present in the returned kwargs dict.
- ``binding_states`` reflects the real ``vault_bindings`` rows.
- ``binding_states`` does NOT include ``local_path`` (userspace).
- ``manifest_summary`` reflects local-index state (no relay/AEAD).
- ``manifest_summary`` is None when the config has no vault id.
- The kwargs round-trip through ``write_debug_bundle`` and the
  produced ZIP contains the two previously-missing entries.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault.binding.bindings import VaultBindingsStore  # noqa: E402
from src.vault.state.local_index import VaultLocalIndex  # noqa: E402


VAULT_ID = "X2Z3EBY3SKVN"
DOCS_ID = "rf_v1_" + "a" * 24


class CollectBundleInputsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_b2_inputs_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.config_dir = self.tmpdir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # Index creates the DB on first construction.
        self.index = VaultLocalIndex(self.config_dir)
        self.store = VaultBindingsStore(self.index.db_path)
        # The Maintenance tab calls collect_bundle_inputs at click
        # time; the worker pulls vault_id_undashed from the GTK
        # context. The helper takes it as a parameter so tests don't
        # need any GTK.
        self.config_data = {
            "server_url": "http://127.0.0.1:4441",
            "allow_logging": True,
            "vault": {"last_known_id": VAULT_ID},
        }

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add_bound_binding(self, *, name: str = "/home/u/folder-a") -> str:
        b = self.store.create_binding(
            vault_id=VAULT_ID, remote_folder_id=DOCS_ID, local_path=name,
        )
        self.store.update_binding_state(
            b.binding_id, state="bound", sync_mode="two-way",
            last_synced_revision=7,
        )
        return b.binding_id

    def test_returns_all_five_bundle_kwargs(self) -> None:
        from src.windows_vault.tab_maintenance import collect_bundle_inputs
        self._add_bound_binding()
        kwargs = collect_bundle_inputs(
            self.config_data, self.config_dir, VAULT_ID,
        )
        # Mirror write_debug_bundle's parameter list — all 5 names
        # present, no extras (catches a regression where a future
        # refactor accidentally drops one).
        self.assertEqual(
            set(kwargs.keys()),
            {
                "config", "db_path", "binding_states",
                "activity_log_path", "manifest_summary",
            },
        )

    def test_binding_states_reflects_real_rows(self) -> None:
        from src.windows_vault.tab_maintenance import collect_bundle_inputs
        bid = self._add_bound_binding()
        kwargs = collect_bundle_inputs(
            self.config_data, self.config_dir, VAULT_ID,
        )
        rows = kwargs["binding_states"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["binding_id"], bid)
        self.assertEqual(row["vault_id"], VAULT_ID)
        self.assertEqual(row["state"], "bound")
        self.assertEqual(row["sync_mode"], "two-way")
        self.assertEqual(row["last_synced_revision"], 7)

    def test_binding_states_does_not_leak_local_path(self) -> None:
        from src.windows_vault.tab_maintenance import collect_bundle_inputs
        self._add_bound_binding(name="/home/secret-user/private-folder")
        kwargs = collect_bundle_inputs(
            self.config_data, self.config_dir, VAULT_ID,
        )
        for row in kwargs["binding_states"]:
            self.assertNotIn(
                "local_path", row,
                "local_path is userspace metadata; must not be in bundle",
            )
        serialized = json.dumps(kwargs["binding_states"])
        self.assertNotIn("secret-user", serialized)
        self.assertNotIn("private-folder", serialized)

    def test_manifest_summary_has_local_only_fields(self) -> None:
        """Manifest summary must be buildable from local DB alone —
        no relay calls, no AEAD decryption. Otherwise a locked vault
        couldn't produce a bundle, defeating the diagnostic value.
        """
        from src.windows_vault.tab_maintenance import collect_bundle_inputs
        # Bump the manifest floor so the summary has a non-zero floor.
        self.index.bump_manifest_revision_floor(VAULT_ID, 42)
        kwargs = collect_bundle_inputs(
            self.config_data, self.config_dir, VAULT_ID,
        )
        summary = kwargs["manifest_summary"]
        self.assertIsNotNone(summary)
        self.assertEqual(summary["vault_id"], VAULT_ID)
        self.assertEqual(summary["manifest_revision_floor"], 42)
        self.assertEqual(summary["cached_folder_count"], 0)
        self.assertEqual(summary["pending_ops_count"], 0)

    def test_manifest_summary_is_none_without_vault_id(self) -> None:
        from src.windows_vault.tab_maintenance import collect_bundle_inputs
        kwargs = collect_bundle_inputs(
            self.config_data, self.config_dir, "",
        )
        self.assertIsNone(kwargs["manifest_summary"])

    def test_activity_log_only_attached_when_file_exists(self) -> None:
        from src.windows_vault.tab_maintenance import collect_bundle_inputs
        kwargs = collect_bundle_inputs(
            self.config_data, self.config_dir, VAULT_ID,
        )
        self.assertIsNone(kwargs["activity_log_path"])

        # Now create the log file and re-collect.
        log_dir = self.config_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "vault.log"
        log_path.write_text("2026-05-19 21:00:00 [INFO] tail line\n")

        kwargs = collect_bundle_inputs(
            self.config_data, self.config_dir, VAULT_ID,
        )
        self.assertEqual(kwargs["activity_log_path"], log_path)

    def test_kwargs_produce_a_5_entry_bundle(self) -> None:
        """End-to-end shape check — the kwargs round-trip through
        ``write_debug_bundle`` and the produced ZIP contains the two
        entries that Suite 0007 B2 caught as missing.
        """
        from src.windows_vault.tab_maintenance import collect_bundle_inputs
        from src.vault.diagnostics.debug_bundle import build_debug_bundle_bytes

        self._add_bound_binding()
        # Seed an activity log so all 5 entries get written.
        log_dir = self.config_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "vault.log").write_text("seeded\n")

        kwargs = collect_bundle_inputs(
            self.config_data, self.config_dir, VAULT_ID,
        )
        raw = build_debug_bundle_bytes(**kwargs)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist())
        self.assertEqual(
            names,
            {
                "config.redacted.json",
                "index_schema.txt",
                "binding_states.json",
                "activity_tail.txt",
                "manifest_summary.json",
            },
            "fix must produce the full 5-entry bundle the UI label "
            "advertises (Suite 0007 B2 found 3/5)",
        )


if __name__ == "__main__":
    unittest.main()
