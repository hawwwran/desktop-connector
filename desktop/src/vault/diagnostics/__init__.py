"""Vault-side diagnostics + debug helpers.

Submodules:
- ``logging`` — per-vault rotating logger ("vault.<vault_id_12>.log")
- ``debug_bundle`` — writer for the user-facing debug bundle (zip with
  redacted manifest + activity excerpt + index + sync state snapshots)
- ``ransomware_detector`` — heuristic that pauses sync when a watcher
  sees a large fraction of in-place rewrites in a short window
"""
