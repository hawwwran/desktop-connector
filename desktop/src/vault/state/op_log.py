"""Producer side of the vault encrypted op-log (Activity-timeline Phase 1).

The consumer side parses entries via :func:`vault.state.activity.normalize_op_log_entry`.
This module is the producer side: it builds entries in the same shape the
consumer already understands, and appends them onto an existing manifest
tail with bounded growth.

Wire shape (matches ``normalize_op_log_entry``)::

    {
        "ts": <int epoch seconds>,
        "type": "vault.upload.completed",
        "device_id": "<32 hex>",
        "revision": <int>,
        # optional:
        "path": "Documents/budget.xlsx",
        "device_name": "Hostname-Laptop",
        "summary": "Uploaded version 3",
        # plus any caller-supplied extras (forwarded into ActivityRow.extra)
    }

The consumer drops entries whose ``type`` doesn't start with one of the
whitelisted prefixes in ``ACTIVITY_KIND_PREFIXES`` (state/activity.py:44),
so producers MUST use a known prefix or the row is silently invisible.
At the API boundary the parameter is named ``event_type`` (so it
doesn't shadow Python's ``type`` builtin) but the on-disk dict key
stays ``"type"`` for wire-format compatibility.

Tail-bounding policy (plan D3): keep the most-recent ``MAX_OP_LOG_TAIL``
entries; drop-oldest beyond that. No rotation into ``archived_op_segments``
in v1 — deferred to v1.1. When truncation drops entries, emit a
``vault.activity.tail_truncated_evicted_oldest count=N`` INFO log so the
eviction is observable in vault.log (plan D4).

Cross-version safety (plan D7): callers MUST pass the prior tail they
want to grow. On CAS-retry paths the prior tail must be re-read from
the **server-side** envelope returned in the 409 conflict, not from the
original attempt — otherwise concurrent producer entries from another
writer get clobbered.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable


log = logging.getLogger(__name__)


# Cap chosen as ``4 × PUBLISH_BATCH_SIZE`` (binding/sync.py:122 is 50) so a
# single full batch lands without immediately evicting the prior batch. At
# ~230 B per entry × 200 = ~46 KB plaintext / ~50 KB after AEAD — small
# enough to leave headroom on the manifest size budget. If
# PUBLISH_BATCH_SIZE changes, revisit this constant.
MAX_OP_LOG_TAIL = 200


_RESERVED_FIELDS = frozenset({
    "ts",
    "type",
    "path",
    "device_id",
    "device_name",
    "summary",
    "revision",
})


def build_op_log_entry(
    *,
    event_type: str,
    device_id: str,
    revision: int,
    path: str = "",
    device_name: str = "",
    summary: str = "",
    ts: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one op-log entry in the wire shape ``normalize_op_log_entry`` parses.

    The on-disk dict key is ``"type"`` (matches the consumer parser at
    ``state/activity.py``); the kwarg is named ``event_type`` so it
    doesn't shadow Python's ``type`` builtin at the call site.

    ``ts`` defaults to the current epoch second. ``extra`` lets a caller
    stash supplementary fields (e.g., ``source_version_id`` on a restore);
    the consumer side surfaces them via ``ActivityRow.extra``.

    A blank ``device_id`` is allowed at the API boundary so unit tests can
    construct fixtures without faking a real device id, but every
    production caller will pass ``config.device_id`` so the manifest's
    ``author_device_id`` and the entry's ``device_id`` agree.
    """
    if not event_type:
        raise ValueError("op-log entry requires non-empty 'event_type'")
    entry: dict[str, Any] = {
        "ts": int(ts if ts is not None else time.time()),
        "type": str(event_type),
        "device_id": str(device_id),
        "revision": int(revision),
    }
    if path:
        entry["path"] = str(path)
    if device_name:
        entry["device_name"] = str(device_name)
    if summary:
        entry["summary"] = str(summary)
    if extra:
        collisions = _RESERVED_FIELDS & set(extra)
        if collisions:
            raise ValueError(
                f"op-log extras collide with reserved fields: "
                f"{sorted(collisions)}"
            )
        entry.update(extra)
    return entry


def append_op_log_entries(
    prior_tail: Iterable[dict[str, Any]] | None,
    new_entries: Iterable[dict[str, Any]] | None,
    *,
    max_tail: int = MAX_OP_LOG_TAIL,
) -> list[dict[str, Any]]:
    """Return ``prior_tail + new_entries``, capped at ``max_tail`` (drop oldest).

    Both inputs tolerate ``None`` so callers don't pre-check freshly-built
    shards. The returned list is a fresh copy — neither input is mutated.

    When the cap drops entries, emits a
    ``vault.activity.tail_truncated_evicted_oldest count=N`` INFO log so the
    eviction is observable in vault.log.

    ``max_tail`` is a kwarg so tests can pin a smaller cap without
    monkeypatching ``MAX_OP_LOG_TAIL``; production code should never
    pass it.
    """
    if max_tail < 0:
        raise ValueError(f"max_tail must be non-negative; got {max_tail}")
    prior = list(prior_tail or [])
    additions = list(new_entries or [])
    combined = prior + additions
    if len(combined) <= max_tail:
        return combined
    evicted = len(combined) - max_tail
    log.info(
        "vault.activity.tail_truncated_evicted_oldest count=%d",
        evicted,
    )
    # combined[-0:] would be the full list — explicit slice avoids the trap.
    return combined[len(combined) - max_tail:]


__all__ = [
    "MAX_OP_LOG_TAIL",
    "append_op_log_entries",
    "build_op_log_entry",
]
