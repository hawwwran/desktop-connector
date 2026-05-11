"""Per-vault local state (caches, activity log, usage stats, file index).

Submodules:
- ``local_index`` — SQLite mirror of the remote manifest (chunks + entries
  + tombstones) for fast list/preflight without re-downloading the manifest
- ``local_state`` — on-disk state file at ``<config_dir>/vault_local_state.json``:
  pending publish, last-known-id, pending-disconnect markers, …
- ``usage`` — quota / used-bytes accounting from the manifest + index
- ``activity`` — sliding-window activity log emitted by sync paths
"""
