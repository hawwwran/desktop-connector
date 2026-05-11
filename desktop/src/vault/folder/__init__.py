"""Folder-level orchestration glue between bindings, the runtime, and the UI.

Submodules:
- ``actions`` — GTK-free dispatchers (pause / resume / disconnect / sync-now)
- ``runtime`` — ``VaultRuntime`` orchestrator (single per open vault)
- ``ui_state`` — Folders-tab view-model: row state, byte/time formatting,
  ignore-pattern parsing
- ``connect_dialog`` — Adw.Dialog for connecting a remote folder to a local
  path; calls into ``binding.preflight`` for the summary panel
"""
