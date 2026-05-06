"""Local Vault disconnect behavior."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.vault_local import disconnect_local_vault  # noqa: E402


VAULT_ID = "ABCD2345WXYZ"


class VaultDisconnectTests(unittest.TestCase):
    def test_disconnect_forgets_local_vault_but_preserves_active_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp))
            config._data["vault"] = {
                "active": True,
                "last_known_id": VAULT_ID,
                "cached_local_state": "remove-me",
            }
            config.save()

            deleted: list[tuple[Path, str]] = []
            disconnected = disconnect_local_vault(
                config,
                grant_deleter=lambda config_dir, vault_id: deleted.append((config_dir, vault_id)),
            )

            self.assertEqual(disconnected, VAULT_ID)
            self.assertEqual(deleted, [(Path(tmp), VAULT_ID)])

            reopened = Config(Path(tmp))
            self.assertEqual(reopened._data.get("vault"), {"active": True})
            self.assertTrue(reopened.vault_active)

    def test_disconnect_removes_pending_local_vault_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            for name in ("vault_migration.json", "vault_pending_purges.json"):
                (config_dir / name).write_text("{}", encoding="utf-8")

            config = Config(config_dir)
            config._data["vault"] = {"active": False, "last_known_id": VAULT_ID}
            config.save()

            disconnect_local_vault(config, grant_deleter=lambda _config_dir, _vault_id: None)

            self.assertEqual(Config(config_dir)._data.get("vault"), {"active": False})
            self.assertFalse((config_dir / "vault_migration.json").exists())
            self.assertFalse((config_dir / "vault_pending_purges.json").exists())


    def test_disconnect_purges_upload_resume_sessions_for_vault(self) -> None:
        """F-D26: a stale upload session for the disconnecting vault is
        deleted; sessions targeting *other* vaults survive untouched.
        Without the purge, reconnecting to the same vault id (recovery
        kit re-import, or a new vault that happens to mint the same id)
        would resurrect a session that PUTs against a relay that no
        longer holds the matching chunks.
        """
        import json
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            os.environ["XDG_CACHE_HOME"] = str(cache_root)
            try:
                resume_dir = (
                    cache_root / "desktop-connector" / "vault" / "uploads"
                )
                resume_dir.mkdir(parents=True, exist_ok=True)
                _write_resume_session(
                    resume_dir, vault_id=VAULT_ID,
                    session_id="ses_doomed",
                )
                other_vault = "ZYXW2345ABCD"
                _write_resume_session(
                    resume_dir, vault_id=other_vault,
                    session_id="ses_other",
                )

                config_dir = Path(tmp) / "config"
                config_dir.mkdir(parents=True)
                config = Config(config_dir)
                config._data["vault"] = {
                    "active": True, "last_known_id": VAULT_ID,
                }
                config.save()

                disconnect_local_vault(
                    config,
                    grant_deleter=lambda _c, _v: None,
                )

                surviving = sorted(p.name for p in resume_dir.glob("*.json"))
                self.assertEqual(surviving, ["ses_other.json"])
            finally:
                os.environ.pop("XDG_CACHE_HOME", None)

    def test_disconnect_purges_per_vault_chunk_cache(self) -> None:
        """F-D26: the disconnected vault's chunk-cache subtree is
        removed; sibling vault directories survive. The chunks are
        AEAD-bound to the disconnected master key — a casual disk
        read can't decrypt — but their existence still leaks size +
        count of vault content over time, and a re-import via
        recovery kit could pair with cached chunks the relay also
        still carries.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            os.environ["XDG_CACHE_HOME"] = str(cache_root)
            try:
                chunks_root = (
                    cache_root / "desktop-connector" / "vault" / "chunks"
                )
                # Production paths are UPPER-case (`normalize_vault_id`
                # canonicalizes) — mirror that in the test fixture.
                doomed = chunks_root / VAULT_ID
                doomed.mkdir(parents=True, exist_ok=True)
                (doomed / "aa").mkdir(parents=True, exist_ok=True)
                (doomed / "aa" / "ch_v1_a_data").write_bytes(b"x")

                other = chunks_root / "ZYXW2345ABCD"
                other.mkdir(parents=True, exist_ok=True)
                (other / "bb").mkdir(parents=True, exist_ok=True)
                (other / "bb" / "ch_v1_b").write_bytes(b"y")

                config_dir = Path(tmp) / "config"
                config_dir.mkdir(parents=True)
                config = Config(config_dir)
                config._data["vault"] = {
                    "active": True, "last_known_id": VAULT_ID,
                }
                config.save()

                disconnect_local_vault(
                    config,
                    grant_deleter=lambda _c, _v: None,
                )

                self.assertFalse(doomed.exists())
                self.assertTrue(other.exists())
                self.assertTrue((other / "bb" / "ch_v1_b").is_file())
            finally:
                os.environ.pop("XDG_CACHE_HOME", None)


def _write_resume_session(
    resume_dir: Path, *, vault_id: str, session_id: str,
) -> None:
    """Write a minimal UploadSession JSON to ``resume_dir``.

    Mirrors the schema ``UploadSession.from_json`` accepts so the F-D26
    purge helper can deserialize, read the ``vault_id`` field, and
    decide whether the session belongs to the disconnecting vault.
    """
    import json
    payload = {
        "session_id": session_id,
        "vault_id": vault_id,
        "remote_folder_id": "rf_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
        "remote_path": "doomed.txt",
        "entry_id": "fe_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
        "version_id": "fv_v1_aaaaaaaaaaaaaaaaaaaaaaaa",
        "author_device_id": "a" * 32,
        "content_fingerprint": "",
        "logical_size": 0,
        "local_path": "/tmp/doomed.txt",
        "chunk_size": 2 * 1024 * 1024,
        "created_at": "2026-05-04T12:00:00.000Z",
        "chunks": [],
        "phase": "uploading",
    }
    (resume_dir / f"{session_id}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
