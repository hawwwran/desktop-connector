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

import copy
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from ..relay_errors import VaultCASConflictError


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


def maybe_genesis_followup_entries(
    parent_root: dict[str, Any] | None,
    *,
    new_revision: int,
    device_id: str,
    ts: int | None = None,
) -> list[dict[str, Any]]:
    """Return ``[vault.create entry]`` iff this root publish is the
    first follow-up after genesis; ``[]`` otherwise.

    The check passes when (a) the parent root's ``root_revision`` is 1
    (genesis published at revision 1 per ``Vault.prepare``) AND (b) the
    parent tail carries no prior ``vault.create`` entry. Plan D5 defers
    the genesis row to the first follow-up rather than mutating the
    genesis envelope itself — patching genesis would be a
    schema-version concern.

    Idempotent across CAS retries: once a ``vault.create`` entry has
    landed in any prior tail, subsequent calls return ``[]`` so a retry
    against a server-side tail that already carries the entry doesn't
    duplicate it. (At the parent_revision==1 check this would only fire
    if a concurrent device raced us to revision 2 with the entry; we
    won't be that device, but the entry will still land on the
    eventual winning publish through one of the contending devices.)
    """
    if not isinstance(parent_root, dict):
        return []
    if int(parent_root.get("root_revision", 0)) != 1:
        return []
    prior_tail = parent_root.get("operation_log_tail") or []
    for entry in prior_tail:
        if isinstance(entry, dict) and entry.get("type") == "vault.create":
            return []
    return [build_op_log_entry(
        event_type="vault.create",
        device_id=device_id,
        revision=int(new_revision),
        ts=ts,
    )]


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


_DEFAULT_AUDIT_PUBLISH_RETRIES = 3


def publish_root_audit_entry(
    *,
    vault: Any,
    relay: Any,
    event_type: str,
    author_device_id: str,
    extra: dict[str, Any] | None = None,
    summary: str = "",
    max_retries: int = _DEFAULT_AUDIT_PUBLISH_RETRIES,
    log_event_prefix: str = "vault.audit_publish",
) -> bool:
    """Fetch root → bump → append one op-log entry → CAS-publish.

    The single shared shape behind every "best-effort post-op audit
    row" caller in Phase 3.1 (``ops/clear.py``, ``ops/eviction.py``,
    ``migration/runner.py``, ``grant/audit.py``). Each call landed a
    duplicate of the same ~50-line fetch/bump/append/retry block;
    this helper holds the single source of truth.

    Behaviour:

    - Skips silently with ``False`` if ``author_device_id`` is empty
      so unattributed rows can't sneak onto the timeline.
    - Rides ``maybe_genesis_followup_entries`` so a first-follow-up
      audit publish also stamps the deferred ``vault.create`` row.
    - On ``VaultCASConflictError`` retries up to ``max_retries``
      times, re-fetching the server head each attempt so a
      concurrent writer's tail entries survive.
    - Best-effort: any other exception is caught, logged at WARN
      with ``log_event_prefix``, and the function returns ``False``.

    ``log_event_prefix`` names the dot-vocabulary used for telemetry —
    e.g. ``"vault.eviction.alarm_audit"`` emits
    ``"vault.eviction.alarm_audit.cas_retry"`` and
    ``"vault.eviction.alarm_audit.failed"``. Keep call-site prefixes
    stable so existing log queries continue to match.
    """
    if not author_device_id:
        return False
    last_exc: Exception | None = None
    vault_id = getattr(vault, "vault_id", "?")
    for attempt in range(max_retries):
        try:
            current_root = vault.fetch_root_manifest(relay)
            parent_revision = int(current_root.get("root_revision", 0))
            new_revision = parent_revision + 1
            timestamp = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z",
            )
            candidate = copy.deepcopy(current_root)
            candidate["root_revision"] = new_revision
            candidate["parent_root_revision"] = parent_revision
            candidate["created_at"] = timestamp
            candidate["author_device_id"] = str(author_device_id)
            create_entries = maybe_genesis_followup_entries(
                current_root,
                new_revision=new_revision,
                device_id=author_device_id,
            )
            candidate["operation_log_tail"] = append_op_log_entries(
                candidate.get("operation_log_tail"),
                [
                    *create_entries,
                    build_op_log_entry(
                        event_type=event_type,
                        device_id=author_device_id,
                        revision=new_revision,
                        summary=summary,
                        extra=extra,
                    ),
                ],
            )
            vault.publish_root_manifest(relay, candidate)
            return True
        except VaultCASConflictError as exc:
            last_exc = exc
            log.info(
                "%s.cas_retry vault=%s event=%s attempt=%d/%d",
                log_event_prefix, vault_id, event_type,
                attempt + 1, max_retries,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            break
    log.warning(
        "%s.failed vault=%s event=%s last_error=%r",
        log_event_prefix, vault_id, event_type, last_exc,
    )
    return False


__all__ = [
    "MAX_OP_LOG_TAIL",
    "append_op_log_entries",
    "build_op_log_entry",
    "maybe_genesis_followup_entries",
    "publish_root_audit_entry",
]
