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

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


# Allowed top-level ``vault_X`` identifiers. Match on the literal name
# right after the ``vault_`` prefix (so ``vault_folders`` is allowed but
# ``vault_folders_v2`` is not — a new package needs its own entry).
ALLOWED_VAULT_PREFIXED = {
    "folders",   # Folders TAB GTK UI, intentionally kept top-level
    "submenu",   # tray/vault_submenu.py — tray-internal helper
}


def _suffix_after_vault_prefix(name: str) -> str | None:
    """Return the underscore-separated suffix of a dotted name whose
    first segment is ``vault_<suffix>``, or ``None`` if the name does
    not start with that prefix.

    Examples:
        ``"vault_runtime"``         → ``"runtime"``
        ``"vault_folders.tab"``     → ``"folders"``
        ``"vault_folders_v2"``      → ``"folders_v2"``
        ``"src.vault_runtime"``     → ``None`` (first segment is ``src``)
        ``"vault.folder"``          → ``None`` (no ``_`` after ``vault``)
    """
    first = name.split(".", 1)[0]
    if not first.startswith("vault_"):
        return None
    return first[len("vault_"):]


def _resolve_relative_package(path: Path, level: int) -> Path | None:
    """Resolve ``from .[.[.]] import X`` to the directory whose
    submodules ``X`` could plausibly name. ``level=1`` is the package
    containing ``path``; ``level=2`` is the parent of that; etc.
    Returns ``None`` if the level walks above the repo root."""
    pkg = path.parent
    # level=1 means "current package" — already at pkg. level=2 means
    # walk up one. level=N walks up N-1 times.
    for _ in range(level - 1):
        if pkg == pkg.parent:
            return None
        pkg = pkg.parent
    return pkg


def _is_submodule_present(pkg_dir: Path, name: str) -> bool:
    """True if ``pkg_dir`` contains a ``name.py`` file or a ``name/``
    subpackage. Used to disambiguate ``from . import vault_X`` between
    a submodule import (file exists → real legacy reintroduction) and
    a re-exported symbol (file absent → harmless ``__init__.py``
    re-export)."""
    if (pkg_dir / f"{name}.py").is_file():
        return True
    if (pkg_dir / name / "__init__.py").is_file():
        return True
    return False


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(line_number, suffix, source_line)`` for every legacy
    ``vault_<X>`` *module-path* reference in ``path``.

    Uses ``ast.parse`` so we don't trip on docstring/log-tag/SQL
    references that happen to contain ``vault_X`` as text. Covers
    every import shape that Python's import statement supports:

    - ``import vault_X``,                  ``import vault_X as Y``
    - ``import src.vault_X``,              ``import src.vault_X as Y``
    - ``from vault_X import Y``,           ``from src.vault_X import Y``
    - ``from .vault_X import Y``,          ``from ..vault_X import Y``
    - ``from . import vault_X``,           ``from .. import vault_X``
    - ``from src import vault_X``,         multi-line ``from X import (\\n  vault_Y,\\n)``

    The walker distinguishes *module paths* (which are the legacy
    paths we want to flag) from *symbol names* (which can legitimately
    start with ``vault_``, e.g. ``vault_id_dashed``,
    ``vault_chunk_cache_path``, ``vault_settings_button_state``).
    Rule for ``from <SRC> import <NAME>``:

    1. ``<SRC>`` is always a module path — always check it.
    2. ``<NAME>`` is only a module path when ``<SRC>`` is itself a
       package and you're naming a submodule. In practice that's just
       the relative ``from . import X`` / ``from .. import X`` form
       and the top-level ``from src import X`` form. Anywhere deeper
       (``from src.vault.ui.ui_state import vault_X``), ``vault_X`` is
       almost always a callable / class / variable, not a module.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    source_lines = source.splitlines()
    out: list[tuple[int, str, str]] = []

    def _record(lineno: int, suffix: str) -> None:
        if suffix in ALLOWED_VAULT_PREFIXED:
            return
        line_text = source_lines[lineno - 1].rstrip() if 0 < lineno <= len(source_lines) else ""
        out.append((lineno, suffix, line_text))

    def _strip_src(name: str) -> str:
        return name[len("src."):] if name.startswith("src.") else name

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # ``import <DOTTED>`` / ``import <DOTTED> as Y`` — DOTTED is
            # always a module path.
            for alias in node.names:
                suffix = _suffix_after_vault_prefix(_strip_src(alias.name))
                if suffix is not None:
                    _record(node.lineno, suffix)
        elif isinstance(node, ast.ImportFrom):
            # (1) The ``from <SRC>`` half is always a module path.
            if node.module:
                suffix = _suffix_after_vault_prefix(_strip_src(node.module))
                if suffix is not None:
                    _record(node.lineno, suffix)
            # (2) The ``import <NAME>`` half names a submodule only when
            #     <SRC> is itself a package, which in practice is just:
            #     - ``from . import …`` / ``from .. import …`` (level > 0
            #       AND node.module is None — purely relative)
            #     - ``from src import …`` (level == 0 AND module == "src")
            #     Deeper sources name symbols, not submodules.
            names_are_submodules = (
                (node.level > 0 and node.module is None)
                or (node.level == 0 and node.module == "src")
            )
            if names_are_submodules:
                # Disambiguate submodule vs ``__init__.py`` re-export by
                # checking the filesystem. ``from .. import X`` where X
                # is a re-exported symbol from the parent ``__init__.py``
                # would otherwise be a false positive (e.g.
                # ``from .. import vault_id_dashed`` inside
                # ``vault/state/local_state.py``).
                if node.level > 0:
                    pkg_dir = _resolve_relative_package(path, node.level)
                else:  # ``from src import …``
                    pkg_dir = path
                    while pkg_dir.parent != pkg_dir and pkg_dir.name != "src":
                        pkg_dir = pkg_dir.parent
                    if pkg_dir.name != "src":
                        pkg_dir = None
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    suffix = _suffix_after_vault_prefix(alias.name)
                    if suffix is None:
                        continue
                    # Only flag if the file actually exists as a
                    # submodule. ``__init__.py`` re-exports of symbols
                    # that happen to start with ``vault_`` are not a
                    # legacy-path regression.
                    if pkg_dir is None or _is_submodule_present(pkg_dir, alias.name):
                        _record(node.lineno, suffix)
    return out


def _scan_for_violations(roots: list[Path]) -> list[tuple[Path, int, str, str]]:
    """Return ``(path, line_number, suffix, source_line)`` tuples for
    every legacy ``vault_<X>`` import across ``roots``."""
    offenders: list[tuple[Path, int, str, str]] = []
    self_path = Path(__file__).resolve()
    for root in roots:
        for p in root.rglob("*.py"):
            # Skip the test file itself — its docstring + examples
            # intentionally mention ``vault_X`` patterns.
            if p.resolve() == self_path:
                continue
            for lineno, suffix, line_text in _scan_file(p):
                offenders.append((p, lineno, suffix, line_text))
    return offenders


class VaultLegacyPathsGuard(unittest.TestCase):
    """Fails if any ``vault_X`` import (in any of Python's import
    statement shapes) re-appears for an ``X`` that isn't in
    ``ALLOWED_VAULT_PREFIXED``.

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


# Self-tests for the scanner itself. The original regex shipped with
# three missed shapes (`from . import X`, `from .. import X`,
# `from src import X`); these cases catch that class of bug at the
# scanner level so a regression in the scanner doesn't silently
# weaken the guard above.
class ScannerSelfCheck(unittest.TestCase):
    """Exercise each import shape the scanner is supposed to catch."""

    def _scan_source(
        self,
        source: str,
        *,
        siblings: tuple[str, ...] = (),
        parent_siblings: tuple[str, ...] = (),
        src_siblings: tuple[str, ...] = (),
    ) -> list[tuple[int, str, str]]:
        """Build a tiny fake package tree and run the scanner against
        the file containing ``source``. The layout is:

            <tmp>/src/
                pkg/
                  parent/
                    file.py            ← contains ``source``
                    {siblings}.py      ← simulate ``from . import X``
                  {parent_siblings}.py ← simulate ``from .. import X``
                {src_siblings}.py      ← simulate ``from src import X``

        Sibling lists let the test exercise the filesystem-aware
        disambiguation (real submodule present → flagged; absent →
        treated as ``__init__.py`` re-export and skipped).
        """
        import shutil
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="vault_guard_self_test_"))
        try:
            src_dir = root / "src"
            pkg_dir = src_dir / "pkg"
            parent_dir = pkg_dir / "parent"
            parent_dir.mkdir(parents=True)
            (src_dir / "__init__.py").write_text("", encoding="utf-8")
            (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
            (parent_dir / "__init__.py").write_text("", encoding="utf-8")
            for name in siblings:
                (parent_dir / f"{name}.py").write_text("", encoding="utf-8")
            for name in parent_siblings:
                (pkg_dir / f"{name}.py").write_text("", encoding="utf-8")
            for name in src_siblings:
                (src_dir / f"{name}.py").write_text("", encoding="utf-8")
            target = parent_dir / "file.py"
            target.write_text(source, encoding="utf-8")
            return _scan_file(target)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # --- positive cases: each shape must be caught --------------------

    def test_from_dot_module(self) -> None:
        hits = self._scan_source("from .vault_runtime import VaultRuntime\n")
        self.assertEqual([(1, "runtime", "from .vault_runtime import VaultRuntime")], hits)

    def test_from_dotdot_module(self) -> None:
        hits = self._scan_source("from ..vault_runtime import VaultRuntime\n")
        self.assertEqual([(1, "runtime", "from ..vault_runtime import VaultRuntime")], hits)

    def test_from_src_dotted_module(self) -> None:
        hits = self._scan_source("from src.vault_runtime import VaultRuntime\n")
        self.assertEqual([(1, "runtime", "from src.vault_runtime import VaultRuntime")], hits)

    def test_import_module(self) -> None:
        hits = self._scan_source("import src.vault_runtime\n")
        self.assertEqual([(1, "runtime", "import src.vault_runtime")], hits)

    def test_import_module_as(self) -> None:
        hits = self._scan_source("import src.vault_runtime as vault_rt\n")
        self.assertEqual([(1, "runtime", "import src.vault_runtime as vault_rt")], hits)

    def test_from_src_namespace_import(self) -> None:
        """``from src import vault_X`` — the original regex MISSED this."""
        hits = self._scan_source(
            "from src import vault_runtime\n",
            src_siblings=("vault_runtime",),
        )
        self.assertEqual([(1, "runtime", "from src import vault_runtime")], hits)

    def test_from_relative_namespace_import(self) -> None:
        """``from . import vault_X`` — the original regex MISSED this."""
        hits = self._scan_source(
            "from . import vault_runtime\n",
            siblings=("vault_runtime",),
        )
        self.assertEqual([(1, "runtime", "from . import vault_runtime")], hits)

    def test_from_relative_dotdot_namespace_import(self) -> None:
        """``from .. import vault_X`` — the original regex MISSED this."""
        hits = self._scan_source(
            "from .. import vault_runtime\n",
            parent_siblings=("vault_runtime",),
        )
        self.assertEqual([(1, "runtime", "from .. import vault_runtime")], hits)

    def test_multiline_import_paren(self) -> None:
        """``from X import (\\n  vault_Y,\\n)`` parens form — also missed
        by line-by-line regex."""
        source = (
            "from . import (\n"
            "    vault_runtime,\n"
            "    vault_crypto,\n"
            ")\n"
        )
        hits = self._scan_source(source, siblings=("vault_runtime", "vault_crypto"))
        # ast reports both names at the statement's lineno (line 1)
        self.assertEqual(
            sorted(hits),
            sorted([(1, "runtime", "from . import ("), (1, "crypto", "from . import (")]),
        )

    # --- filesystem-aware disambiguation ------------------------------

    def test_reexported_symbol_not_flagged(self) -> None:
        """``from . import vault_X`` where no ``vault_X.py`` exists is
        an ``__init__.py`` re-export of a symbol that happens to start
        with ``vault_`` — not a legacy module-path regression. The
        scanner must NOT flag it.

        Real-world case: ``from .. import vault_id_dashed`` inside
        ``vault/state/local_state.py`` (vault_id_dashed is re-exported
        from ``vault/__init__.py``, not a submodule)."""
        hits = self._scan_source(
            "from .. import vault_id_dashed\n",
            # parent_siblings is empty — no vault_id_dashed.py exists
        )
        self.assertEqual([], hits)

    def test_real_submodule_still_flagged(self) -> None:
        """Same shape as above, but the file IS present → flagged."""
        hits = self._scan_source(
            "from .. import vault_runtime\n",
            parent_siblings=("vault_runtime",),
        )
        self.assertEqual([(1, "runtime", "from .. import vault_runtime")], hits)

    # --- allowlist behaviour ------------------------------------------

    def test_allowed_folders_passes(self) -> None:
        hits = self._scan_source("from .vault_folders import build_vault_folders_tab\n")
        self.assertEqual([], hits)

    def test_allowed_folders_subpath_passes(self) -> None:
        hits = self._scan_source("from src.vault_folders.tab import build\n")
        self.assertEqual([], hits)

    def test_allowed_submenu_passes(self) -> None:
        hits = self._scan_source("from .vault_submenu import VaultSubmenuMixin\n")
        self.assertEqual([], hits)

    def test_folders_v2_extension_rejected(self) -> None:
        """``vault_folders_v2`` does NOT match the allowed ``folders``
        — the allowlist is exact, not prefix-based, so a new top-level
        module needs its own explicit allowlist entry."""
        hits = self._scan_source("from .vault_folders_v2 import X\n")
        self.assertEqual([(1, "folders_v2", "from .vault_folders_v2 import X")], hits)

    # --- non-import noise: the scanner must NOT trip --------------------

    def test_docstring_mention_ignored(self) -> None:
        source = '"""Once upon a time we had from .vault_runtime import …"""\n'
        self.assertEqual([], self._scan_source(source))

    def test_log_tag_string_ignored(self) -> None:
        source = 'log.warning("vault_runtime.foo failed")\n'
        self.assertEqual([], self._scan_source(source))

    def test_sql_identifier_ignored(self) -> None:
        source = 'db.execute("CREATE TABLE vault_bindings (id TEXT)")\n'
        self.assertEqual([], self._scan_source(source))

    def test_consolidated_vault_dot_module_ignored(self) -> None:
        """The NEW path uses ``vault.X`` (dot), not ``vault_X`` (underscore).
        Make sure the scanner doesn't confuse them."""
        source = (
            "from src.vault.binding.runtime import VaultRuntime\n"
            "from .vault.crypto import normalize_vault_id\n"
            "from ..vault import Vault\n"
        )
        self.assertEqual([], self._scan_source(source))

    # --- syntax errors are tolerated (no crash) -----------------------

    def test_syntax_error_file_skipped_silently(self) -> None:
        """Half-written files in a working tree must not break the
        guard — ast.parse raises SyntaxError, which we swallow."""
        hits = self._scan_source("def broken(:\n")
        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()
