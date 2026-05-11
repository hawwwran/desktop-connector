"""Cross-relay vault migration state machine.

Submodules:
- ``migration`` — durable state at ``<config_dir>/vault_migration.json``:
  ``MigrationRecord``, ``load_state`` / ``save_state``, transition table
- ``runner`` — orchestrates start / verify / commit / switch-back against
  both relays; sources of truth for transition logging
- ``propagation`` — cross-device propagation helpers, ``can_switch_back``,
  ``PREVIOUS_RELAY_GRACE_DAYS``
"""
