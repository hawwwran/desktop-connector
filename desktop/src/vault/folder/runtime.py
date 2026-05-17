"""F-518 — VaultRuntime for the Folders tab.

The Folders tab used to spawn worker threads that opened the local
vault, called ``Vault.add_remote_folder`` / ``Vault.rename_remote_folder``
/ ``flush_and_sync_binding`` directly, and threaded a per-tab
``threading.Lock`` through every callsite. The tab thus mixed three
concerns: GTK widget code, worker-thread plumbing, and vault-mutation
business logic.

This module collapses the third layer into a small, GTK-free
:class:`VaultRuntime` that:

- holds the per-runtime serialization lock (was ``_vault_lock`` in
  the tab — see F-517 for why this exists);
- opens and closes the local vault around every operation, so the
  ``master_key`` lifetime is always scoped to a single op;
- exposes named operations the tab calls instead of raw ``Vault.*``
  methods (``fetch_manifest``, ``add_remote_folder``,
  ``rename_remote_folder``, ``flush_and_sync_binding``,
  ``run_initial_baseline``).

The tab keeps owning GTK widget mutation, worker-thread spawning,
and ``GLib.idle_add`` result forwarding — those are GTK-shaped and
don't generalize. What changes:

- One small object replaces the per-tab lock + context manager +
  five inline ``open_local_vault_from_grant`` callsites.
- The runtime is unit-testable without GTK, libadwaita, or
  threading — :func:`_open_serialized` accepts an ``opener``
  injection point.
- Future tabs (Devices, Activity, Maintenance) can either reuse the
  same runtime or compose alongside it without re-deriving the
  serialization story.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from ..binding.baseline import run_initial_baseline as _run_initial_baseline
from ..binding.sync import flush_and_sync_binding as _flush_and_sync_binding
from ..binding.bindings import VaultBindingsStore
from ..binding.runtime import (
    create_vault_relay as _create_vault_relay,
    open_local_vault_from_grant as _open_local_vault_from_grant,
)


VaultOpener = Callable[[Path, Any, str], Any]
RelayFactory = Callable[[Any], Any]


class VaultRuntime:
    """Per-tab serialization + named operations against the local vault.

    Construct once per Folders tab. Each operation:
      1. Acquires :attr:`_lock` so two overlapping clicks can't keep
         two ``master_key`` copies in memory at once.
      2. Reloads ``config`` (so wizard-subprocess writes show up
         across boundaries).
      3. Opens the local vault from the grant store.
      4. Runs the op and closes the vault — the ``master_key`` is
         zeroed by ``Vault.close`` on the way out, regardless of
         whether the op raised.
      5. Releases the lock.

    ``opener`` and ``relay_factory`` are injection points for tests
    so the runtime can be exercised against fakes; production callers
    use the defaults.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        config,
        vault_id: str,
        local_index,
        opener: VaultOpener | None = None,
        relay_factory: RelayFactory | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._config = config
        self._vault_id = vault_id
        self._local_index = local_index
        self._lock = threading.Lock()
        self._opener = opener or _open_local_vault_from_grant
        self._relay_factory = relay_factory or _create_vault_relay

    @property
    def vault_id(self) -> str:
        return self._vault_id

    @property
    def local_index(self):
        return self._local_index

    @contextmanager
    def _open_serialized(self):
        """Context manager that owns lock acquisition + vault open/close.

        Exit path closes the vault before releasing the lock so
        ``master_key`` is zero'd while the next waiter is still
        blocked. Acquisition order matches the pre-F-518 tab.
        """
        self._lock.acquire()
        try:
            self._config.reload()
            vault = self._opener(self._config_dir, self._config, self._vault_id)
        except Exception:
            self._lock.release()
            raise
        try:
            yield vault
        finally:
            try:
                vault.close()
            finally:
                self._lock.release()

    def fetch_manifest(self) -> dict:
        """Read the current unified manifest. Used by usage-refresh and
        the Connect-folder dialog precondition.

        Phase H step 7b: backed by ``vault.fetch_unified_manifest``
        (root + per-folder shards, assembled into the legacy unified
        shape) so callers' walks over ``remote_folders`` /
        ``entries`` keep working. The ``fetch_unified_manifest``
        wrapper itself disappears in step 7f along with the legacy
        shape — at that point the runtime exposes ``fetch_root`` +
        ``fetch_folder_shard`` directly and callers walk shards
        lazily.
        """
        relay = self._relay_factory(self._config)
        with self._open_serialized() as vault:
            return vault.fetch_unified_manifest(relay, local_index=self._local_index)

    def add_remote_folder(
        self,
        *,
        display_name: str,
        ignore_patterns: list[str],
        author_device_id: str,
    ) -> dict:
        """Append a new remote folder to the head manifest."""
        relay = self._relay_factory(self._config)
        with self._open_serialized() as vault:
            return vault.add_remote_folder(
                relay,
                display_name=display_name,
                ignore_patterns=ignore_patterns,
                author_device_id=author_device_id,
                local_index=self._local_index,
            )

    def rename_remote_folder(
        self,
        *,
        remote_folder_id: str,
        new_display_name: str,
        author_device_id: str,
    ) -> dict:
        """Rename an existing remote folder via a manifest CAS."""
        relay = self._relay_factory(self._config)
        with self._open_serialized() as vault:
            return vault.rename_remote_folder(
                relay,
                remote_folder_id=remote_folder_id,
                new_display_name=new_display_name,
                author_device_id=author_device_id,
                local_index=self._local_index,
            )

    def update_remote_folder_settings(
        self,
        *,
        remote_folder_id: str,
        author_device_id: str,
        new_display_name: str | None = None,
        ignore_patterns: list[str] | None = None,
    ) -> dict:
        """Update editable remote-folder settings (name + ignore patterns)
        via a single manifest CAS. Lets the Folders tab's Configure
        dialog change patterns after creation, which the rename-only
        path didn't allow.
        """
        relay = self._relay_factory(self._config)
        with self._open_serialized() as vault:
            return vault.update_remote_folder_settings(
                relay,
                remote_folder_id=remote_folder_id,
                author_device_id=author_device_id,
                new_display_name=new_display_name,
                ignore_patterns=ignore_patterns,
                local_index=self._local_index,
            )

    def flush_and_sync_binding(
        self,
        *,
        binding_id: str,
        author_device_id: str,
        device_name: str,
        should_continue: Callable[[], bool],
    ):
        """Drive a Sync-now / post-Resume flush for a binding.

        Loads the binding row inside the same vault-open scope so the
        store's view is consistent with the manifest the worker uses.
        Re-raises ``RuntimeError`` if the binding row vanished.
        """
        relay = self._relay_factory(self._config)
        store = VaultBindingsStore(self._local_index.db_path)
        with self._open_serialized() as vault:
            binding = store.get_binding(binding_id)
            if binding is None:
                raise RuntimeError(f"binding not found: {binding_id}")
            return _flush_and_sync_binding(
                vault=vault,
                relay=relay,
                store=store,
                binding=binding,
                author_device_id=author_device_id,
                device_name=device_name,
                should_continue=should_continue,
            )

    def run_initial_baseline(self, *, record) -> None:
        """Run the initial baseline for a freshly created binding.

        Holds the vault lock across both the manifest fetch and the
        baseline scan so the binding can't be sync'd by a sibling
        Sync-now worker mid-baseline (overlapping master_key copies
        + concurrent local-disk writes for the same binding root).
        """
        relay = self._relay_factory(self._config)
        with self._open_serialized() as vault:
            manifest = vault.fetch_unified_manifest(
                relay, local_index=self._local_index,
            )
            store = VaultBindingsStore(self._local_index.db_path)
            binding = store.get_binding(record.binding_id)
            if binding is None:
                raise RuntimeError(
                    f"binding row vanished: {record.binding_id}"
                )
            _run_initial_baseline(
                vault=vault,
                relay=relay,
                manifest=manifest,
                store=store,
                binding=binding,
            )


__all__ = ["VaultRuntime"]
