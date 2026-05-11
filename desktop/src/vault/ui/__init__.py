"""GTK-free UI helpers shared across the vault windows + tray.

Submodules:
- ``browser_model`` — manifest-decryption + tree/file-list model the
  browser window consumes (also used by the migration + import runners
  for decrypt-side helpers)
- ``ui_state`` — generic view-model primitives (sorted-by selectors,
  bound display labels, …)
- ``window_args`` — argv parser shared by every ``python3 -m src.windows``
  subprocess entrypoint
- ``bytes_format`` — binary-prefix (KiB/MiB/GiB/TiB) byte formatter
- ``time_format`` — short/long relative timestamps
"""
