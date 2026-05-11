"""Import-bundle parsing + merge runner.

The package name carries the trailing-underscore convention (``import_/``)
because Python's ``import`` keyword forbids the bare form.

Submodules:
- ``bundle`` — bundle parsing, manifest merge logic, typed errors
- ``runner`` — wizard-facing orchestrator: preview, merge, publish
"""
