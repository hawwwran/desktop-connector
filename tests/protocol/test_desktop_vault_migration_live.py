"""B3 — Vault migration round-trip against real PHP relays.

Drives :func:`vault.migration.runner.run_migration` end-to-end across
TWO hermetic :class:`_ServerHarness` instances (relay A on a random
port, relay B on another), exercising the HTTP layer instead of the
``FakeMigrationRelay`` used by the unit tests. The reason for the
duplication: the migration runner unit tests pin the engine's state
machine, but they don't catch HTTP-layer or PHP-side regressions
(e.g. a server-side schema drift or response-envelope mismatch).

This test is intentionally slow — Argon2id (even at reduced params)
+ two PHP server boots + a multi-step migration walk costs single-
digit seconds — so it lives alongside the streaming-integration test
as a "real network" suite member.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.binding.runtime import VaultHttpRelay  # noqa: E402
from src.vault.migration.runner import run_migration  # noqa: E402

from test_server_contract import _ServerHarness  # noqa: E402


# Argon2id at minimum-credible params: ~64 MiB / 2 iters runs in ~150ms
# on this host. Default 128 MiB / 4 iters costs ~600ms per derive; with
# two derives in the test (create + recover later if added) the test
# would budget ~1.5s on Argon2id alone.
TEST_ARGON_MEMORY_KIB = 65_536
TEST_ARGON_ITERATIONS = 2


@dataclass
class _MiniConfig:
    """Minimum surface VaultHttpRelay reads from its config arg.

    The real Config also has ``last_known_vault_id`` etc., but
    VaultHttpRelay only touches ``server_url`` / ``device_id`` /
    ``auth_token`` plus the ``reload()`` no-op hook and a ``save()``
    sink for the migration-propagation side-effect (writes the
    migrated-to URL into the relay's config). In test scope we
    no-op both — propagation correctness is covered by
    ``test_desktop_vault_migration_propagation.py``.
    """
    server_url: str
    device_id: str
    auth_token: str

    def reload(self) -> None:
        return None

    def save(self) -> None:
        return None


def _register_device(harness: _ServerHarness) -> tuple[str, str]:
    """Register a desktop device against the harness; return (device_id, token)."""
    import base64
    import secrets
    status, _h, body = harness.request(
        "POST",
        "/api/devices/register",
        json_body={
            "public_key": base64.b64encode(secrets.token_bytes(32)).decode(),
            "device_type": "desktop",
        },
    )
    if status not in (200, 201):
        raise RuntimeError(f"device register failed: {status} {body}")
    return body["device_id"], body["auth_token"]


def _make_relay(harness: _ServerHarness) -> tuple[VaultHttpRelay, str]:
    """Build a VaultHttpRelay pointed at ``harness``. Returns the
    relay client plus the device_id it registered with (callers use
    this as ``author_device_id`` so the server-side envelope-author
    check passes)."""
    device_id, token = _register_device(harness)
    config = _MiniConfig(
        server_url=harness.base_url,
        device_id=device_id,
        auth_token=token,
    )
    return VaultHttpRelay(config), device_id


class VaultMigrationLiveTests(unittest.TestCase):
    """End-to-end migration across two real PHP relays."""

    @classmethod
    def setUpClass(cls) -> None:
        # `migrationAllowPrivateUrls=true` lets the relay accept the
        # other harness's 127.0.0.1:<random-port> URL as a legitimate
        # migration target. Dev/test rigs flip this; production
        # deployments keep the default (false).
        cls.relay_a_harness = _ServerHarness(
            config_overrides={"migrationAllowPrivateUrls": True},
        )
        cls.relay_a_harness.start()
        cls.relay_b_harness = _ServerHarness(
            config_overrides={"migrationAllowPrivateUrls": True},
        )
        cls.relay_b_harness.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.relay_a_harness.stop()
        cls.relay_b_harness.stop()

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="dc-b3-live-"))
        self.config_dir_a = self.tmpdir / "cfg-a"
        self.config_dir_b = self.tmpdir / "cfg-b"
        self.config_dir_a.mkdir(parents=True)
        self.config_dir_b.mkdir(parents=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _bootstrap_vault_on_relay_a(self) -> tuple[Vault, VaultHttpRelay, VaultHttpRelay, str, str]:
        """Spin up two VaultHttpRelay clients (A + B), create + publish
        a vault on A. Returns (vault, relay_a, relay_b, dev_a, dev_b)."""
        relay_a, dev_a = _make_relay(self.relay_a_harness)
        relay_b, dev_b = _make_relay(self.relay_b_harness)
        vault = Vault.create_new(
            relay_a, "correct-horse-battery-staple",
            argon_memory_kib=TEST_ARGON_MEMORY_KIB,
            argon_iterations=TEST_ARGON_ITERATIONS,
        )
        return vault, relay_a, relay_b, dev_a, dev_b

    def test_genesis_vault_round_trip_a_to_b(self) -> None:
        """Migrate a fresh (genesis) vault A→B against two real PHP
        relays; verify the engine + HTTP integration end-to-end with
        no chunk content.

        Scope is intentionally narrow (genesis vault, no folder
        publishes, no file uploads) so the test stays under the
        server-side ``Config::vaultAuthLimit()`` (default 10 calls /
        minute / (device,vault) pair; floor 10 — see ADR 2026-05-19).
        With files + uploads the limit trips before migration starts;
        the engine path is the same in both cases, so genesis-only is
        enough to catch HTTP-layer regressions (the broader chunk-copy
        scenario is covered by ``test_desktop_vault_migration_runner.py``'s
        FakeMigrationRelay suite).
        """
        vault, relay_a, relay_b, dev_a, dev_b = self._bootstrap_vault_on_relay_a()
        try:
            result = run_migration(
                vault=vault,
                source_relay=relay_a,
                target_relay=relay_b,
                source_relay_url=self.relay_a_harness.base_url,
                target_relay_url=self.relay_b_harness.base_url,
                config_dir=self.config_dir_a,
            )
            self.assertTrue(
                result.verify.matches,
                f"A→B verify mismatches: {result.verify.mismatches}",
            )
            # Genesis vault: no chunks to copy.
            self.assertEqual(result.chunks_copied, 0)
            self.assertEqual(result.chunks_skipped, 0)

            # Sanity: target carries the same root revision after migration.
            root_on_a = vault.fetch_root_manifest(relay_a)
            root_on_b = vault.fetch_root_manifest(relay_b)
            self.assertEqual(
                int(root_on_a["root_revision"]),
                int(root_on_b["root_revision"]),
                "post-migration root revision diverged between A and B",
            )

            # The source has recorded ``migrated_to`` (commit step). A
            # subsequent get_header on relay A should still succeed (the
            # vault row stays — only flagged migrated, not deleted).
            header_after = relay_a.get_header(vault.vault_id, vault.vault_access_secret)
            self.assertIn("encrypted_header", header_after)
        finally:
            try:
                vault.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
