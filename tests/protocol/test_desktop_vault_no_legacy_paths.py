"""Regression guard: catch re-introduction of pre-consolidation
``vault_*`` import paths.

Waves A–G (2026-05-09 → 2026-05-11) folded every flat ``vault_*.py``
module and the ``vault_upload/`` / ``vault_download/`` packages under
``desktop/src/vault/``. A future contributor could silently start
fragmenting the namespace again by writing ``desktop/src/vault_new.py``
and importing it as ``from .vault_new import …`` — this test fails
loudly if that happens.

The intentional survivors are listed in ``ALLOWED_VAULT_PREFIXED``:

- ``vault_folders/`` — the Folders TAB GTK widget tree. ``vault.folder``
  (singular) is the data layer; ``vault_folders/`` is UI. The
  duplication is documented in ``docs/plans/post-breakup-followups.md``.
- ``vault_submenu`` — the tray's vault submenu mixin
  (``tray/vault_submenu.py``). Tray-internal, not a vault-subsystem
  module.

If you add a new legitimate top-level ``vault_*`` package or module,
extend ``ALLOWED_VAULT_PREFIXED`` here.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


# Allowed top-level ``vault_X`` identifiers — anything else in an import
# path is a regression. Match on the underscore-separated token after the
# ``vault_`` prefix (so ``vault_folders`` matches but ``vault_runtime``
# does not).
ALLOWED_VAULT_PREFIXED = {
    "folders",   # Folders TAB GTK UI, intentionally kept top-level
    "submenu",   # tray/vault_submenu.py — tray-internal helper
}


# Match the bare identifier ``vault_<name>`` in any import-statement
# shape (``from .vault_X``, ``from ..vault_X``, ``from src.vault_X``,
# ``import src.vault_X``, ``import src.vault_X as Y``). We scan source
# lines, not arbitrary strings, so docstring/log-tag/SQL references that
# happen to contain ``vault_X`` won't trigger.
_IMPORT_LINE = re.compile(
    r"""
    ^\s*                          # leading indent
    (?:from\s+\S*\.|              # ``from .vault_X``, ``from ..vault_X``, ``from src.vault_X``, etc.
       from\s+src\s+|             # ``from src import vault_X as …``
       import\s+(?:src\.)?)       # ``import vault_X`` / ``import src.vault_X``
    vault_(\w+)                   # capture the suffix
    \b
    """,
    re.VERBOSE,
)


def _scan_for_violations(roots: list[Path]) -> list[tuple[Path, int, str, str]]:
    """Return a list of (path, line_number, suffix, line_text) tuples
    for every ``vault_<X>`` import where ``X`` is not in
    ``ALLOWED_VAULT_PREFIXED``."""
    offenders: list[tuple[Path, int, str, str]] = []
    for root in roots:
        for p in root.rglob("*.py"):
            # Skip the test file itself — it intentionally mentions
            # ``vault_X`` patterns in its own source (this docstring,
            # the regex, the allowlist).
            if p.resolve() == Path(__file__).resolve():
                continue
            try:
                for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    m = _IMPORT_LINE.match(line)
                    if not m:
                        continue
                    suffix = m.group(1)
                    # Allowed top-level modules — strip after first
                    # underscore (e.g. ``folders`` from ``folders/``).
                    top = suffix.split("_", 1)[0] if "_" not in suffix else suffix
                    # Direct allowlist hit (e.g. suffix == "folders")
                    if suffix in ALLOWED_VAULT_PREFIXED:
                        continue
                    # Sub-path under an allowed package
                    # (e.g. ``vault_folders.tab`` — but that'd show up as
                    # ``from src.vault_folders.tab import …`` and the
                    # captured suffix is still ``folders``, fine).
                    if top in ALLOWED_VAULT_PREFIXED:
                        continue
                    offenders.append((p, lineno, suffix, line.rstrip()))
            except (OSError, UnicodeDecodeError):
                continue
    return offenders


class VaultLegacyPathsGuard(unittest.TestCase):
    """Fails if any ``from … vault_X import …`` import re-appears for an
    ``X`` that isn't in ``ALLOWED_VAULT_PREFIXED``.

    To fix a failure: rewrite the import to the consolidated path
    (``from src.vault.<sub>.<module> import …``), OR if you're
    introducing a new legitimate ``vault_X`` package, extend
    ``ALLOWED_VAULT_PREFIXED`` above with a comment explaining why.
    """

    def test_no_legacy_vault_paths_in_desktop_or_tests(self) -> None:
        roots = [
            Path(REPO_ROOT, "desktop", "src"),
            Path(REPO_ROOT, "tests", "protocol"),
        ]
        offenders = _scan_for_violations(roots)
        if offenders:
            lines = [
                f"  {p.relative_to(REPO_ROOT)}:{lineno}  ({suffix!r})  {text}"
                for p, lineno, suffix, text in offenders
            ]
            self.fail(
                "Pre-consolidation `vault_X` import paths have re-appeared. "
                "Rewrite them to the new `src.vault.<sub>.<module>` form, "
                "OR add the suffix to ALLOWED_VAULT_PREFIXED if intentional.\n"
                + "\n".join(lines)
            )


if __name__ == "__main__":
    unittest.main()
