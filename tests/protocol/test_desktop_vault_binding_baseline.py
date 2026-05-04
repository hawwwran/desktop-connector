"""T10.3 — Initial baseline download + entry seeding."""

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

from src.vault import Vault  # noqa: E402
from src.vault_bindings import VaultBindingsStore  # noqa: E402
from src.vault_binding_baseline import (  # noqa: E402
    BaselineProgress,
    run_initial_baseline,
)
from src.vault_cache import VaultLocalIndex  # noqa: E402
from src.vault_crypto import DefaultVaultCrypto  # noqa: E402
from src.vault_manifest import (  # noqa: E402
    make_manifest,
    make_remote_folder,
    tombstone_file_entry,
)
from src.vault_upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import FakeUploadRelay  # noqa: E402


VAULT_ACCESS_SECRET = "vault-secret"


class VaultBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_baseline_test_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")
        self.config_dir = self.tmpdir / "config"
        self.local_root = self.tmpdir / "Documents"
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.index = VaultLocalIndex(self.config_dir)
        self.store = VaultBindingsStore(self.index.db_path)

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_remote(self, files: dict[str, bytes], *, with_tombstone: str | None = None) -> tuple[FakeUploadRelay, dict]:
        manifest = make_manifest(
            vault_id=VAULT_ID,
            revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-04T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[],
                )
            ],
        )
        relay = FakeUploadRelay(manifest=manifest)
        vault = _vault()
        try:
            current = manifest
            for path, content in files.items():
                local = self.tmpdir / "src_" / path.replace("/", "_")
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(content)
                res = upload_file(
                    vault=vault, relay=relay, manifest=current,
                    local_path=local, remote_folder_id=DOCS_ID,
                    remote_path=path, author_device_id=AUTHOR,
                )
                current = res.manifest
            if with_tombstone is not None:
                current = tombstone_file_entry(
                    current,
                    remote_folder_id=DOCS_ID,
                    path=with_tombstone,
                    deleted_at="2026-05-04T13:00:00.000Z",
                    author_device_id=AUTHOR,
                )
                current["revision"] = int(current["revision"]) + 1
                current["parent_revision"] = current["revision"] - 1
                vault.publish_manifest(relay, current)
        finally:
            vault.close()
        from src.vault_browser_model import decrypt_manifest as _decrypt
        observer = _vault()
        try:
            published = _decrypt(observer, relay.current_envelope)
        finally:
            observer.close()
        return relay, published

    def test_baseline_materializes_remote_files_and_seeds_entries(self) -> None:
        """T10.3 acceptance: After baseline, binding_state = bound; local
        files match remote current state."""
        payload_a = b"alpha content for baseline test"
        payload_b = b"beta content under nested path"
        relay, manifest = self._seed_remote({
            "alpha.txt": payload_a,
            "nested/beta.txt": payload_b,
        })
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )

        progress: list[BaselineProgress] = []
        vault = _vault()
        try:
            result = run_initial_baseline(
                vault=vault, relay=relay, manifest=manifest,
                store=self.store, binding=binding,
                progress=progress.append,
            )
        finally:
            vault.close()

        # Local files match remote bytes.
        self.assertEqual(
            (self.local_root / "alpha.txt").read_bytes(), payload_a,
        )
        self.assertEqual(
            (self.local_root / "nested" / "beta.txt").read_bytes(), payload_b,
        )
        # Binding flipped to bound + revision stamped.
        rebound = self.store.get_binding(binding.binding_id)
        self.assertEqual(rebound.state, "bound")
        self.assertEqual(rebound.last_synced_revision, int(manifest["revision"]))
        # Local-entries seeded.
        relatives = {e.relative_path for e in self.store.list_local_entries(binding.binding_id)}
        self.assertIn("alpha.txt", relatives)
        self.assertIn("nested/beta.txt", relatives)
        # Progress fires "done" last.
        self.assertEqual(progress[-1].phase, "done")
        # Result fields aligned.
        self.assertEqual(set(result.downloaded_files), {"alpha.txt", "nested/beta.txt"})
        self.assertEqual(result.extra_files, [])

    def test_pre_existing_local_files_become_extras_not_deleted(self) -> None:
        """T10.3 acceptance: no deletions of pre-existing local files;
        those become 'extra' in vault_local_entries."""
        relay, manifest = self._seed_remote({"remote-only.txt": b"remote bytes"})
        # Pre-place a local file the remote doesn't know about.
        (self.local_root / "local-only.txt").write_bytes(b"already here")
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        vault = _vault()
        try:
            result = run_initial_baseline(
                vault=vault, relay=relay, manifest=manifest,
                store=self.store, binding=binding,
            )
        finally:
            vault.close()

        # The local file survives.
        self.assertEqual(
            (self.local_root / "local-only.txt").read_bytes(),
            b"already here",
        )
        # Recorded as an extra (last_synced_revision=0 + empty fingerprint).
        entries = {
            e.relative_path: e
            for e in self.store.list_local_entries(binding.binding_id)
        }
        self.assertIn("local-only.txt", entries)
        self.assertEqual(entries["local-only.txt"].last_synced_revision, 0)
        self.assertEqual(entries["local-only.txt"].content_fingerprint, "")
        self.assertEqual(result.extra_files, ["local-only.txt"])

    def test_tombstones_skipped_during_baseline(self) -> None:
        """§D15: tombstones never produce local file deletions before
        the binding's initial baseline is captured."""
        relay, manifest = self._seed_remote(
            {"keep.txt": b"keep me", "ghost.txt": b"will be tombstoned"},
            with_tombstone="ghost.txt",
        )
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(self.local_root),
        )
        vault = _vault()
        try:
            result = run_initial_baseline(
                vault=vault, relay=relay, manifest=manifest,
                store=self.store, binding=binding,
            )
        finally:
            vault.close()

        # The tombstoned file is NOT materialized locally.
        self.assertFalse((self.local_root / "ghost.txt").exists())
        # The non-tombstoned file IS materialized.
        self.assertTrue((self.local_root / "keep.txt").is_file())
        self.assertNotIn("ghost.txt", result.downloaded_files)
        self.assertIn("keep.txt", result.downloaded_files)

    def test_unknown_remote_folder_raises_keyerror(self) -> None:
        relay, manifest = self._seed_remote({"x.txt": b"x"})
        binding = self.store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id="rf_v1_z" * 5,    # not in manifest
            local_path=str(self.local_root),
        )
        vault = _vault()
        try:
            with self.assertRaises(KeyError):
                run_initial_baseline(
                    vault=vault, relay=relay, manifest=manifest,
                    store=self.store, binding=binding,
                )
        finally:
            vault.close()


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID, master_key=MASTER_KEY,
        recovery_secret=None, vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1, manifest_revision=1,
        manifest_ciphertext=b"", crypto=DefaultVaultCrypto,
    )


if __name__ == "__main__":
    unittest.main()
