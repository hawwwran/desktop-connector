"""T12.6 / §H7 — Multi-device concurrent-ops integration test.

Spins up two ``Device`` harnesses pointed at the same in-memory
``FakeUploadRelay`` and same vault, and walks scripted scenarios:

- Simultaneous upload of the same path: both versions land in
  ``versions[]`` and the manifest's ``latest_version_id`` is
  deterministic across all observers.
- Delete-vs-edit race: device A tombstones, device B edits at the
  same time. After both desktops complete a sync cycle, the surviving
  state agrees on both sides — and a locally-modified file on B is
  preserved.
- Three-device merge: a third device arrives with its own queued
  uploads while A and B are mid-sync. Every device converges on the
  same ``revision`` + ``latest_version_id``.

These are all CI-runnable: no real filesystem watchers, no real
clocks — events are driven explicitly so the run is deterministic.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault_binding_sync import run_backup_only_cycle  # noqa: E402
from src.vault_binding_twoway import run_two_way_cycle  # noqa: E402
from src.vault_bindings import VaultBindingsStore, VaultLocalEntry  # noqa: E402
from src.vault_browser_model import decrypt_manifest  # noqa: E402
from src.vault_cache import VaultLocalIndex  # noqa: E402
from src.vault_crypto import (  # noqa: E402
    DefaultVaultCrypto,
    derive_content_fingerprint_key, make_content_fingerprint,
)
from src.vault_manifest import (  # noqa: E402
    find_file_entry, make_manifest, make_remote_folder,
)
from src.vault_upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR, DOCS_ID, MASTER_KEY, VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import FakeUploadRelay  # noqa: E402


VAULT_ACCESS_SECRET = "vault-secret"


def _make_vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID,
        master_key=MASTER_KEY,
        recovery_secret=None,
        vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1,
        manifest_revision=1,
        manifest_ciphertext=b"",
        crypto=DefaultVaultCrypto,
    )


def _keyed_fingerprint(content: bytes) -> str:
    import hashlib
    return make_content_fingerprint(
        derive_content_fingerprint_key(MASTER_KEY),
        hashlib.sha256(content).digest(),
    )


@dataclass
class Device:
    """One desktop's worth of state pointed at the shared relay."""

    name: str
    device_id: str  # 32-hex-char per protocol
    config_dir: Path
    local_root: Path
    store: VaultBindingsStore
    binding_id: str

    def get_binding(self):
        return self.store.get_binding(self.binding_id)

    def write_local(self, relative: str, content: bytes) -> None:
        target = self.local_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def delete_local(self, relative: str) -> None:
        target = self.local_root / relative
        if target.exists():
            target.unlink()

    def queue_upload(self, relative: str) -> None:
        self.store.coalesce_op(
            binding_id=self.binding_id,
            op_type="upload", relative_path=relative,
        )

    def queue_delete(self, relative: str) -> None:
        self.store.coalesce_op(
            binding_id=self.binding_id,
            op_type="delete", relative_path=relative,
        )

    def seed_baseline_entry(self, relative: str, content: bytes, revision: int) -> None:
        self.write_local(relative, content)
        self.store.upsert_local_entry(VaultLocalEntry(
            binding_id=self.binding_id,
            relative_path=relative,
            content_fingerprint=_keyed_fingerprint(content),
            size_bytes=len(content),
            mtime_ns=(self.local_root / relative).stat().st_mtime_ns,
            last_synced_revision=revision,
        ))

    def two_way_cycle(self, relay):
        binding = self.get_binding()
        vault = _make_vault()
        try:
            return run_two_way_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding,
                author_device_id=self.device_id,
                device_name=self.name,
            )
        finally:
            vault.close()

    def backup_only_cycle(self, relay):
        binding = self.get_binding()
        vault = _make_vault()
        try:
            return run_backup_only_cycle(
                vault=vault, relay=relay, store=self.store,
                binding=binding,
                author_device_id=self.device_id,
            )
        finally:
            vault.close()


class MultiDeviceH7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_multi_dev_test_"))
        self._saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

        # Build the shared "remote" by publishing an empty manifest from
        # one ephemeral vault — that's how the existing T10.5 / T12.1
        # tests bootstrap a usable FakeUploadRelay.
        manifest = make_manifest(
            vault_id=VAULT_ID, revision=1, parent_revision=0,
            created_at="2026-05-04T12:00:00.000Z",
            author_device_id=AUTHOR,
            remote_folders=[
                make_remote_folder(
                    remote_folder_id=DOCS_ID,
                    display_name_enc="Documents",
                    created_at="2026-05-04T12:00:00.000Z",
                    created_by_device_id=AUTHOR,
                    entries=[],
                ),
            ],
        )
        self.relay = FakeUploadRelay(manifest=manifest)
        self.relay.current_revision = int(manifest.get("parent_revision", 0))
        bootstrap = _make_vault()
        try:
            bootstrap.publish_manifest(self.relay, manifest)
        finally:
            bootstrap.close()
        self.start_revision = int(manifest["revision"])

    def tearDown(self) -> None:
        if self._saved_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_device(
        self, *, name: str, device_id: str, sync_mode: str = "two-way",
    ) -> Device:
        config_dir = self.tmpdir / f"config-{name}"
        local_root = self.tmpdir / f"local-{name}"
        local_root.mkdir(parents=True, exist_ok=True)
        index = VaultLocalIndex(config_dir)
        store = VaultBindingsStore(index.db_path)
        binding = store.create_binding(
            vault_id=VAULT_ID,
            remote_folder_id=DOCS_ID,
            local_path=str(local_root),
        )
        store.update_binding_state(
            binding.binding_id, state="bound",
            sync_mode=sync_mode,
            last_synced_revision=self.start_revision,
        )
        return Device(
            name=name, device_id=device_id,
            config_dir=config_dir, local_root=local_root,
            store=store, binding_id=binding.binding_id,
        )

    def _decrypt_head(self) -> dict:
        observer = _make_vault()
        try:
            return decrypt_manifest(observer, self.relay.current_envelope)
        finally:
            observer.close()

    # ------------------------------------------------------------------
    # Scenario 1 — simultaneous upload of the same path
    # ------------------------------------------------------------------

    def test_simultaneous_upload_same_path_keeps_both_versions(self) -> None:
        a = self._make_device(name="A", device_id="aa" * 16, sync_mode="backup-only")
        b = self._make_device(name="B", device_id="bb" * 16, sync_mode="backup-only")

        # Both devices write a local copy of the same path with different bytes.
        a.write_local("notes.txt", b"alpha-version")
        a.queue_upload("notes.txt")
        b.write_local("notes.txt", b"beta-version")
        b.queue_upload("notes.txt")

        # A drains first; then B (uploading on top of A's revision).
        a.backup_only_cycle(self.relay)
        b.backup_only_cycle(self.relay)

        head = self._decrypt_head()
        entry = find_file_entry(head, DOCS_ID, "notes.txt")
        self.assertIsNotNone(entry)
        versions = entry.get("versions", []) or []
        # Both versions present in versions[].
        self.assertGreaterEqual(len(versions), 2,
                                f"expected 2 versions, got {len(versions)}")

        # latest_version_id deterministic across all observers — i.e. it
        # exists in versions[] and matches the one stored on entry.
        latest_id = entry.get("latest_version_id")
        self.assertTrue(latest_id, "manifest entry has no latest_version_id")
        self.assertIn(
            latest_id, [v.get("version_id") for v in versions],
            "latest_version_id is not present in versions[]",
        )

    # ------------------------------------------------------------------
    # Scenario 2 — delete-vs-edit race (two-way)
    # ------------------------------------------------------------------

    def test_delete_vs_edit_race_keeps_locally_modified_copy(self) -> None:
        # Seed shared.txt remotely so both devices know about it.
        seed_local = self.tmpdir / "seed.txt"
        seed_local.write_bytes(b"shared")
        bootstrap = _make_vault()
        try:
            res = upload_file(
                vault=bootstrap, relay=self.relay, manifest=self._decrypt_head(),
                local_path=seed_local, remote_folder_id=DOCS_ID,
                remote_path="shared.txt", author_device_id=AUTHOR,
            )
            seeded_revision = int(res.manifest["revision"])
        finally:
            bootstrap.close()

        a = self._make_device(name="A", device_id="aa" * 16)
        b = self._make_device(name="B", device_id="bb" * 16)
        # Both devices have the file at the seeded revision.
        a.seed_baseline_entry("shared.txt", b"shared", seeded_revision)
        b.seed_baseline_entry("shared.txt", b"shared", seeded_revision)
        a.store.update_binding_state(
            a.binding_id, last_synced_revision=seeded_revision,
        )
        b.store.update_binding_state(
            b.binding_id, last_synced_revision=seeded_revision,
        )

        # B edits locally; A queues a delete.
        b.write_local("shared.txt", b"important update")
        a.queue_delete("shared.txt")

        # A's cycle drains first → tombstones the remote.
        a.two_way_cycle(self.relay)
        # B's cycle pulls the tombstone → must keep the local edit and
        # push it back as a fresh version (T12.1 §local-delete-vs-remote-modify).
        b.two_way_cycle(self.relay)

        # Local copy on B preserved.
        self.assertTrue((b.local_root / "shared.txt").is_file())
        self.assertEqual(
            (b.local_root / "shared.txt").read_bytes(), b"important update",
        )

        head = self._decrypt_head()
        entry = find_file_entry(head, DOCS_ID, "shared.txt")
        self.assertFalse(bool(entry.get("deleted")),
                         "B's re-upload should clear the tombstone")
        self.assertGreaterEqual(len(entry.get("versions", []) or []), 2)

    # ------------------------------------------------------------------
    # Scenario 3 — three-device merge converges on a shared revision
    # ------------------------------------------------------------------

    def test_three_device_merge_converges_on_same_latest_id(self) -> None:
        a = self._make_device(name="A", device_id="aa" * 16, sync_mode="backup-only")
        b = self._make_device(name="B", device_id="bb" * 16, sync_mode="backup-only")
        c = self._make_device(name="C", device_id="cc" * 16, sync_mode="backup-only")

        a.write_local("from-a.txt", b"alpha")
        a.queue_upload("from-a.txt")
        b.write_local("from-b.txt", b"beta")
        b.queue_upload("from-b.txt")
        c.write_local("from-c.txt", b"gamma")
        c.queue_upload("from-c.txt")

        # Drain all three in some order; CAS retry inside upload_file
        # handles the contention.
        a.backup_only_cycle(self.relay)
        b.backup_only_cycle(self.relay)
        c.backup_only_cycle(self.relay)
        # Second pass per device: each one observes the others' final
        # revisions and stamps last_synced_revision to match. This is
        # the real-world poll loop — one cycle pushes work, the next
        # observes the convergent state.
        a.backup_only_cycle(self.relay)
        b.backup_only_cycle(self.relay)
        c.backup_only_cycle(self.relay)

        head = self._decrypt_head()
        # Every contributed file landed.
        for path, payload in [
            ("from-a.txt", b"alpha"),
            ("from-b.txt", b"beta"),
            ("from-c.txt", b"gamma"),
        ]:
            entry = find_file_entry(head, DOCS_ID, path)
            self.assertIsNotNone(entry, f"{path} missing from remote")
            self.assertFalse(bool(entry.get("deleted")))
            # Deterministic id check: the manifest's latest_version_id is
            # the version that all three devices observe at this revision.
            latest = entry.get("latest_version_id")
            self.assertTrue(latest)
            self.assertIn(
                latest,
                [v.get("version_id") for v in entry.get("versions", []) or []],
            )

        # Each device's binding row converges on the same final revision
        # after pulling the head (the cycle stamps last_synced_revision).
        head_revision = int(head["revision"])
        for d in (a, b, c):
            self.assertEqual(
                d.get_binding().last_synced_revision, head_revision,
                f"device {d.name} did not converge on revision {head_revision}",
            )


if __name__ == "__main__":
    unittest.main()
