"""F-LT11 — fresh-unlock stamp for sensitive vault operations.

The architecture doc §13 calls for fresh unlock on sensitive
operations (clear vault, hard purge, rotate access secret, revoke
device, rotate recovery, import-merge into existing vault)
**regardless of the unlock timeout setting**. The §3.9 / §3.11 risk
evaluation surfaced that the codebase did not enforce this — the
wizards loaded the cached device grant without re-prompting for
the passphrase.

This module is the per-process trust anchor: a single in-memory
timestamp (``_last_unlock_at``) set by the mini-prompt
(``windows_vault.fresh_unlock_prompt``) after the user re-types
the recovery passphrase and Argon2id verifies it against the
recovery envelope. Subsequent sensitive operations call
``require_fresh_unlock(operation_label)`` at their handler entry;
if the stamp is missing or expired the handler shows the mini-
prompt before proceeding.

In-memory only — never persisted. A subprocess restart drops the
stamp. Each subprocess has its own stamp (the Vault Settings
window's tab_recovery success path stamps the same process the
tab_danger handlers run in; the import wizard is a separate
subprocess and gets its own stamp).
"""

from __future__ import annotations

import time
from typing import Callable

#: How long a single fresh-unlock stamp remains active. Long enough
#: for the user to walk through the typed-confirm UI after typing
#: the passphrase; short enough that an unattended desk doesn't
#: leave the gate effectively open.
#:
#: Review §2.H3: aligned with ``docs/vault-architecture.md`` §13
#: ("default unlock timeout is 15 min idle"). Pre-fix the window
#: was 120 s — chained destructive ops (revoke device → rotate
#: access secret → schedule purge) past two minutes re-prompted for
#: the passphrase twice or more, which both irritates the user and
#: weakens the security signal (the user starts treating the prompt
#: as noise). The longer window applies to the SAME process only:
#: a process restart re-locks because ``_last_unlock_at`` is module
#: state, not persisted.
FRESH_UNLOCK_WINDOW_S: float = 900.0

_clock: Callable[[], float] = time.monotonic
_last_unlock_at: float | None = None


def stamp_fresh_unlock() -> None:
    """Record that the user just verified the recovery passphrase.

    Subsequent calls to :func:`require_fresh_unlock` within
    :data:`FRESH_UNLOCK_WINDOW_S` succeed. Callers:

    - :mod:`desktop.src.windows_vault.fresh_unlock_prompt` after a
      successful mini-prompt Argon2id verification.
    - :mod:`desktop.src.windows_vault.tab_recovery` after the
      explicit recovery-test dialog reports success (the user just
      typed the passphrase + had it verified — same proof).
    """
    global _last_unlock_at
    _last_unlock_at = _clock()


def is_fresh_unlock_active() -> bool:
    """``True`` when a stamp exists and has not yet expired."""
    if _last_unlock_at is None:
        return False
    return (_clock() - _last_unlock_at) < FRESH_UNLOCK_WINDOW_S


def seconds_remaining() -> float:
    """Seconds left on the active stamp; ``0.0`` if none / expired.

    Useful for surfacing a countdown in the UI when the user is
    about to chain multiple sensitive ops back-to-back.
    """
    if _last_unlock_at is None:
        return 0.0
    return max(0.0, FRESH_UNLOCK_WINDOW_S - (_clock() - _last_unlock_at))


def clear_fresh_unlock() -> None:
    """Drop any active stamp. Call on quit / screen lock / explicit
    "Lock now" UX. Production callers don't need to call this on
    every quit — the subprocess dying drops the in-memory state."""
    global _last_unlock_at
    _last_unlock_at = None


def require_fresh_unlock(operation: str = "") -> None:
    """Raise :class:`FreshUnlockRequiredError` if no active stamp.

    Gate sites call this *before* opening the typed-confirm dialog
    or kicking off the merge-commit worker. The caller handles the
    exception by surfacing the mini-prompt and retrying on
    successful re-verification.
    """
    if not is_fresh_unlock_active():
        from .relay_errors import FreshUnlockRequiredError
        raise FreshUnlockRequiredError(operation=operation)


def _set_clock_for_tests(clock: Callable[[], float]) -> None:
    """Test helper — inject a monotonic-shaped fake clock. Reset via
    :func:`_reset_for_tests` between tests."""
    global _clock
    _clock = clock


def _reset_for_tests() -> None:
    """Test helper — drop the stamp + restore the real clock."""
    global _clock, _last_unlock_at
    _clock = time.monotonic
    _last_unlock_at = None
