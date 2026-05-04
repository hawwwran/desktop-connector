"""T17.3 — Vault integrity check (quick + full)."""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault_integrity import (  # noqa: E402
    IntegrityIssue, IntegrityReport,
    run_full_check, run_quick_check,
)


VAULT_ID = "ABCD2345WXYZ"
SECRET = "vault-secret"
MASTER_KEY = b"\x10" * 32


def _manifest_with_chunks(*chunk_ids: str, parent_revision: int = 0, revision: int = 1) -> dict:
    return {
        "vault_id": VAULT_ID,
        "revision": revision,
        "parent_revision": parent_revision,
        "remote_folders": [
            {
                "remote_folder_id": "rf_v1_xxxxxxxxxxxxxxxxxxxxxxxx",
                "display_name_enc": "Documents",
                "entries": [
                    {
                        "type": "file",
                        "path": "alpha.txt",
                        "deleted": False,
                        "versions": [
                            {
                                "version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                                "logical_size": 16,
                                "content_fingerprint": "fp",
                                "chunks": [
                                    {"chunk_id": cid} for cid in chunk_ids
                                ],
                            }
                        ],
                        "latest_version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
                    }
                ],
            }
        ],
    }


class _FakeVault:
    vault_id = VAULT_ID
    master_key = MASTER_KEY
    vault_access_secret = SECRET

    def __init__(self, manifest):
        self._manifest = manifest

    def fetch_manifest(self, relay, *, local_index=None):
        return self._manifest


class _FakeRelay:
    def __init__(self, *, present_chunks=()):
        self._present = set(present_chunks)
        self._revisions: list[dict] = []

    def batch_head_chunks(self, vault_id, secret, chunk_ids):
        return {
            cid: {"present": cid in self._present}
            for cid in chunk_ids
        }

    def list_manifest_revisions(self, vault_id, secret):
        return list(self._revisions)


class QuickCheckTests(unittest.TestCase):
    def test_clean_vault_passes(self) -> None:
        manifest = _manifest_with_chunks("ch_v1_a", "ch_v1_b", revision=2, parent_revision=1)
        vault = _FakeVault(manifest)
        relay = _FakeRelay(present_chunks=("ch_v1_a", "ch_v1_b"))
        report = run_quick_check(vault=vault, relay=relay)
        self.assertTrue(report.ok, report.broken)
        self.assertEqual(report.scope, "quick")
        self.assertEqual(report.revisions_checked, 1)
        self.assertEqual(report.chunks_checked, 2)

    def test_missing_chunk_reported(self) -> None:
        manifest = _manifest_with_chunks("ch_v1_a", "ch_v1_b", revision=2, parent_revision=1)
        vault = _FakeVault(manifest)
        relay = _FakeRelay(present_chunks=("ch_v1_a",))  # ch_v1_b missing
        report = run_quick_check(vault=vault, relay=relay)
        self.assertFalse(report.ok)
        kinds = [i.kind for i in report.broken]
        self.assertEqual(kinds, ["chunk_missing"])
        self.assertEqual(report.broken[0].target, "ch_v1_b")

    def test_chain_break_reported(self) -> None:
        # parent_revision=0 but revision=5 → chain isn't continuous.
        manifest = _manifest_with_chunks("ch_v1_a", revision=5, parent_revision=0)
        vault = _FakeVault(manifest)
        relay = _FakeRelay(present_chunks=("ch_v1_a",))
        report = run_quick_check(vault=vault, relay=relay)
        self.assertFalse(report.ok)
        self.assertEqual(report.broken[0].kind, "manifest_chain_broken")

    def test_locked_vault_reports_and_returns(self) -> None:
        @dataclass
        class _Locked:
            vault_id: str = VAULT_ID
            master_key = None
            vault_access_secret = None
            def fetch_manifest(self, *_args, **_kwargs):
                self.fetched = True
                return {}
        vault = _Locked()
        relay = _FakeRelay()
        report = run_quick_check(vault=vault, relay=relay)
        self.assertFalse(report.ok)
        self.assertEqual(report.broken[0].kind, "vault_locked")

    def test_tombstoned_entries_skipped(self) -> None:
        manifest = _manifest_with_chunks("ch_v1_a", revision=2, parent_revision=1)
        manifest["remote_folders"][0]["entries"][0]["deleted"] = True
        vault = _FakeVault(manifest)
        relay = _FakeRelay()  # no chunks present
        report = run_quick_check(vault=vault, relay=relay)
        self.assertTrue(report.ok)
        self.assertEqual(report.chunks_checked, 0)


class FullCheckTests(unittest.TestCase):
    def test_full_check_decrypts_every_chunk(self) -> None:
        manifest = _manifest_with_chunks("ch_v1_a", "ch_v1_b", revision=2, parent_revision=1)
        vault = _FakeVault(manifest)
        relay = _FakeRelay(present_chunks=("ch_v1_a", "ch_v1_b"))

        decrypted: list[str] = []
        def decrypt(folder, entry, version, encrypted):
            decrypted.append(encrypted.decode())
            return b"plaintext"
        def fetch(vault_id, secret, cid):
            return cid.encode()

        report = run_full_check(
            vault=vault, relay=relay,
            decrypt_chunk=decrypt, fetch_chunk=fetch,
        )
        self.assertTrue(report.ok, report.broken)
        self.assertEqual(report.scope, "full")
        self.assertEqual(sorted(decrypted), ["ch_v1_a", "ch_v1_b"])

    def test_full_check_reports_aead_failure_per_chunk(self) -> None:
        manifest = _manifest_with_chunks("ch_v1_a", "ch_v1_b", revision=2, parent_revision=1)
        vault = _FakeVault(manifest)
        relay = _FakeRelay(present_chunks=("ch_v1_a", "ch_v1_b"))

        def decrypt(folder, entry, version, encrypted):
            if encrypted == b"ch_v1_b":
                raise ValueError("aead tag mismatch")
            return b"ok"
        def fetch(vault_id, secret, cid):
            return cid.encode()

        report = run_full_check(
            vault=vault, relay=relay,
            decrypt_chunk=decrypt, fetch_chunk=fetch,
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.broken), 1)
        self.assertEqual(report.broken[0].kind, "chunk_aead_failed")
        self.assertEqual(report.broken[0].target, "ch_v1_b")

    def test_full_check_reports_chunk_fetch_failure(self) -> None:
        manifest = _manifest_with_chunks("ch_v1_x", revision=2, parent_revision=1)
        vault = _FakeVault(manifest)
        relay = _FakeRelay(present_chunks=("ch_v1_x",))

        def decrypt(folder, entry, version, encrypted):
            return b""
        def fetch(vault_id, secret, cid):
            raise OSError("connection refused")

        report = run_full_check(
            vault=vault, relay=relay,
            decrypt_chunk=decrypt, fetch_chunk=fetch,
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.broken[0].kind, "chunk_fetch_failed")


if __name__ == "__main__":
    unittest.main()
