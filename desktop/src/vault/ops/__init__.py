"""Data-plane operations on an open vault — anything that mutates
manifest entries or chunk storage but isn't day-to-day sync.

Submodules:
- ``restore`` — pull historical or deleted versions back to local
- ``clear`` — empty a vault while preserving the vault id + grants
- ``repair`` — repair-on-mismatch helper for the manifest store
- ``integrity`` — full integrity check (chunk SHA, manifest hash,
  envelope-version, dangling-ref scan)
- ``eviction`` — quota-driven version eviction (preserve-latest)
- ``delete`` — soft delete (tombstone) + hard delete (chunk GC)
- ``purge_schedule`` — durable schedule for §A20 hard-purge runs
- ``trash`` — local trash helper (move-to-trash with conflict rename)
"""
