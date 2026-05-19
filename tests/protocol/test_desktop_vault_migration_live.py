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

# B3 switch-back: each migration leg makes ~5 auth-billed calls per
# ``(device, vault)`` pair (migration_start + get_header + get_root +
# create_vault + migration_commit). Two back-to-back legs cost roughly
# 5+2 per pair (the second leg reuses the same vault id, so the same
# pairs accumulate). The ADR floor is 10 — that's right on the edge
# before verify-step retries. 30 leaves comfortable headroom and is
# below any cap an operator would consider unsafe.
B3_SWITCH_BACK_AUTH_LIMIT = 30

# Logger name the propagation side-effect emits under — used by the
# switch-back test's ``assertLogs`` capture to pin the leg-direction
# of the §H2 grace-window URL.
MIGRATION_PROPAGATION_LOGGER = "src.vault.binding.runtime"


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


class VaultMigrationLiveSwitchBackTests(unittest.TestCase):
    """B3 follow-up: full round-trip A→B then B→A across two real PHP relays.

    Genesis-leg coverage landed in :class:`VaultMigrationLiveTests`
    (`live-testing-followup.partly.md` §14, 2026-05-19). The switch-back
    leg was deferred there because two back-to-back migrations on the
    same ``(device, vault)`` pair brushed up against the hardcoded
    per-(device, vault) auth limit. The 2026-05-19 ``vaultAuthLimit``
    config-knob ADR removed the floor and gave operators a knob; this
    test bumps the cap to 30 (well above the ~5 auth calls per leg per
    pair) and runs both legs back-to-back in a single test so the
    second hop's verify + propagation invariants are pinned.

    Distinct ``setUpClass`` (vs. the genesis class) because the
    harness's config_overrides are class-scoped — re-using the genesis
    class's harnesses would have left every test sharing the bumped
    limit, which makes the genesis test's "we fit under floor=10"
    assertion lose teeth.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # ``vaultAuthLimit=B3_SWITCH_BACK_AUTH_LIMIT`` is well above the
        # ~5 auth-billed calls each leg makes against each ``(device,
        # vault)`` pair. The ADR floor of 10 (Config
        # ``VAULT_AUTH_LIMIT_FLOOR``) is preserved by the server so this
        # bump is opt-in and reversible per deployment.
        overrides = {
            "migrationAllowPrivateUrls": True,
            "vaultAuthLimit": B3_SWITCH_BACK_AUTH_LIMIT,
        }
        cls.relay_a_harness = _ServerHarness(config_overrides=overrides)
        cls.relay_a_harness.start()
        cls.relay_b_harness = _ServerHarness(config_overrides=overrides)
        cls.relay_b_harness.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.relay_a_harness.stop()
        cls.relay_b_harness.stop()

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="dc-b3-switchback-"))
        # Each leg writes its own ``vault_migration.json`` state to a
        # dedicated dir. ``run_migration`` clears the file on success so
        # a shared dir would also work, but keeping them separate makes
        # post-mortem on a failure obvious — the leg-1 file should be
        # absent (cleared) and leg-2's reflects the final state.
        self.config_dir_leg1 = self.tmpdir / "leg1-a-to-b"
        self.config_dir_leg2 = self.tmpdir / "leg2-b-to-a"
        self.config_dir_leg1.mkdir(parents=True)
        self.config_dir_leg2.mkdir(parents=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_round_trip_a_to_b_then_b_to_a(self) -> None:
        """Migrate A→B, then immediately B→A. Pin the switch-back
        invariants:

        - ``verify.matches`` on both legs.
        - Root revision parity between A and B after each commit.
        - ``get_header`` still resolves on both relays after the
          round-trip (no relay deletes the vault row at commit; the
          row is flagged migrated and stays for the §H2 7-day
          switch-back grace window).
        - The migration runner's ``vault_already_exists`` idempotent
          re-entry handles the case where the target relay still has
          the vault row from the *previous* leg (A had it from
          ``create_new``; on leg 2 the engine re-encounters it as
          target and must NOT crash).
        - The §H2 ``previous_relay_url`` propagation log line **inverts
          on the second hop** — leg 1 records ``previous=A_url``,
          leg 2 records ``previous=B_url``. Without this assertion the
          test would only prove the engine doesn't crash; with it,
          the round-trip's symmetry is pinned.

        Note on scope: ``chunks_copied=0`` on both legs is a *scope*
        assertion (genesis-only test by design — no folder publishes,
        no file uploads), not an engine invariant. The broader
        chunk-copy contract is covered by
        ``test_desktop_vault_migration_runner.py``'s
        ``FakeMigrationRelay`` suite.
        """
        relay_a, _dev_a = _make_relay(self.relay_a_harness)
        relay_b, _dev_b = _make_relay(self.relay_b_harness)
        vault = Vault.create_new(
            relay_a, "correct-horse-battery-staple",
            argon_memory_kib=TEST_ARGON_MEMORY_KIB,
            argon_iterations=TEST_ARGON_ITERATIONS,
        )
        relay_a_url = self.relay_a_harness.base_url
        relay_b_url = self.relay_b_harness.base_url
        try:
            # ── leg 1: A → B (genesis) ─────────────────────────────
            leg1 = run_migration(
                vault=vault,
                source_relay=relay_a,
                target_relay=relay_b,
                source_relay_url=relay_a_url,
                target_relay_url=relay_b_url,
                config_dir=self.config_dir_leg1,
            )
            self.assertTrue(
                leg1.verify.matches,
                f"leg 1 A→B verify mismatches: {leg1.verify.mismatches}",
            )
            # Scope assertion (see docstring): genesis-only test.
            self.assertEqual(leg1.chunks_copied, 0)

            # §H2 grace-window propagation fires on the next ``get_header``
            # against the source relay (the migrated_to field shows up
            # there, and ``VaultHttpRelay._maybe_propagate_relay_migration``
            # is the consumer — runtime.py:213-255). Calling it explicitly
            # here makes the assertion deterministic instead of relying on
            # an incidental call elsewhere in the test.
            with self.assertLogs(MIGRATION_PROPAGATION_LOGGER, "WARNING") as cm1:
                relay_a.get_header(vault.vault_id, vault.vault_access_secret)
            self.assertTrue(
                any(
                    f"new={relay_b_url}" in r.getMessage()
                    and f"previous={relay_a_url}" in r.getMessage()
                    for r in cm1.records
                ),
                "leg 1 must log migration_propagation_applied with "
                f"new={relay_b_url} previous={relay_a_url}; got "
                f"{[r.getMessage() for r in cm1.records]}",
            )

            root_a_post_leg1 = vault.fetch_root_manifest(relay_a)
            root_b_post_leg1 = vault.fetch_root_manifest(relay_b)
            self.assertEqual(
                int(root_a_post_leg1["root_revision"]),
                int(root_b_post_leg1["root_revision"]),
                "post-leg-1 root revision diverged between A and B",
            )

            # ── leg 2: B → A (switch-back) ─────────────────────────
            # Reuse the same vault object (vault_id + master_key +
            # vault_access_secret are stable) but flip the relay
            # orientation. The engine on leg 2 hits
            # ``_bootstrap_target_and_inventory``'s ``vault_already_exists``
            # catch on A — A still has the vault row from
            # ``Vault.create_new`` (flagged migrated_to=B, but the row
            # itself stays).
            leg2 = run_migration(
                vault=vault,
                source_relay=relay_b,
                target_relay=relay_a,
                source_relay_url=relay_b_url,
                target_relay_url=relay_a_url,
                config_dir=self.config_dir_leg2,
            )
            self.assertTrue(
                leg2.verify.matches,
                f"leg 2 B→A verify mismatches: {leg2.verify.mismatches}",
            )
            self.assertEqual(leg2.chunks_copied, 0)

            # §H2 grace-window propagation INVERTS on the second hop:
            # B now has migrated_to=A, so ``relay_b.get_header`` triggers
            # ``previous=B_url`` (the URL we're leaving this time, which
            # was leg 1's target). This is the core "round-trip symmetry"
            # assertion — without it the test only proves the engine
            # doesn't crash on the second leg.
            with self.assertLogs(MIGRATION_PROPAGATION_LOGGER, "WARNING") as cm2:
                relay_b.get_header(vault.vault_id, vault.vault_access_secret)
            self.assertTrue(
                any(
                    f"new={relay_a_url}" in r.getMessage()
                    and f"previous={relay_b_url}" in r.getMessage()
                    for r in cm2.records
                ),
                "leg 2 must log migration_propagation_applied with "
                f"new={relay_a_url} previous={relay_b_url} (inverted "
                f"from leg 1); got {[r.getMessage() for r in cm2.records]}",
            )

            root_a_post_leg2 = vault.fetch_root_manifest(relay_a)
            root_b_post_leg2 = vault.fetch_root_manifest(relay_b)
            self.assertEqual(
                int(root_a_post_leg2["root_revision"]),
                int(root_b_post_leg2["root_revision"]),
                "post-leg-2 root revision diverged between A and B",
            )

            # Both relays still serve the header after the round-trip —
            # the §H2 grace window depends on this for "Switch back to
            # previous relay" to be reachable from the desktop UI. The
            # in-memory ``_MiniConfig.server_url`` was mutated by the
            # propagation handler in the assertLogs blocks above, so
            # these calls resolve as ``already_on_target`` and emit no
            # log — that's the correct steady-state behaviour.
            header_a = relay_a.get_header(
                vault.vault_id, vault.vault_access_secret,
            )
            header_b = relay_b.get_header(
                vault.vault_id, vault.vault_access_secret,
            )
            self.assertIn("encrypted_header", header_a)
            self.assertIn("encrypted_header", header_b)
        finally:
            try:
                vault.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
