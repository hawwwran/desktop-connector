"""fnmatch-based subset of gitignore semantics for binding ignore lists.

Covers the §gaps §7 default list (``*.tmp``, ``~$*``, ``node_modules/``,
``foo/bar``) but rejects ``**`` and rooted ``/foo`` shapes — they
silently never match under fnmatch and would mislead the user. F-D14
emits ``vault.sync.ignore_pattern_unsupported_shape`` once per process
when one of those is encountered so the user has a breadcrumb.

A future migration to ``pathspec`` (gitignore-compatible) would close
this gap properly.
"""

import fnmatch
import logging
from typing import Iterable

log = logging.getLogger(__name__)


_UNSUPPORTED_PATTERN_WARNED: set[str] = set()


def _warn_unsupported_pattern(pat: str) -> None:
    """F-D14: surface unsupported pattern shapes once-per-process.

    ``**`` and rooted ``/foo`` patterns silently fail the fnmatch
    check; without a warning the user sees their pattern "not match
    anything" with no breadcrumb. A future migration to ``pathspec``
    (gitignore-compatible) would close this gap properly; for now we
    at least make the limit visible.
    """
    if pat in _UNSUPPORTED_PATTERN_WARNED:
        return
    _UNSUPPORTED_PATTERN_WARNED.add(pat)
    log.warning(
        "vault.sync.ignore_pattern_unsupported_shape pattern=%r "
        "reason=fnmatch-only "
        "hint=\"**\" and rooted \"/foo\" patterns are not yet "
        "supported — match the leaf name or include the relative "
        "path explicitly",
        pat,
    )


def _matches_ignore(
    name: str,
    rel_path: str,
    patterns: Iterable[str],
    *,
    is_dir: bool,
) -> bool:
    """Subset of gitignore semantics covering the §gaps §7 default list:

    - ``pattern/`` — matches a directory by its leaf name; subtree pruned.
    - ``pattern`` — matches a file or directory by leaf name.
    - ``*.ext`` / ``~$*`` — fnmatch glob against the leaf.
    - ``foo/bar`` — slash-bearing pattern: fnmatch against the relative
      path so ``a/b.txt`` patterns work for nested config.

    Negation, ``**`` and rooted ``/foo`` patterns are not yet supported
    — the §7 defaults don't need them and v1.5 can extend if a
    user-written pattern requires more. F-D14: emit
    ``vault.sync.ignore_pattern_unsupported_shape`` once per process
    when one of those patterns is encountered so the user has a
    breadcrumb explaining why their rule didn't match.
    """
    rel_unix = str(rel_path).replace("\\", "/")
    for raw in patterns:
        pat = str(raw).strip()
        if not pat or pat.startswith("#"):
            continue
        if "**" in pat or pat.startswith("/"):
            _warn_unsupported_pattern(pat)
            # Don't try to match — fnmatch returns nonsense for these
            # shapes; falling through would silently never match.
            continue
        is_dir_pat = pat.endswith("/")
        if is_dir_pat:
            pat = pat[:-1]
        if "/" in pat:
            if fnmatch.fnmatch(rel_unix, pat):
                if not is_dir_pat or is_dir:
                    return True
            continue
        if is_dir_pat and not is_dir:
            continue
        if fnmatch.fnmatch(name, pat):
            return True
    return False
