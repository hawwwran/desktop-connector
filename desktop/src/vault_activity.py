"""Activity-tab data layer (T17.1).

The Activity tab in Vault settings renders the timeline of "major
ops" — create, upload, delete, restore, clear, device grant +
revocation, migration, eviction, purge. The bytes live on the relay
in two places: ``vault_audit_events`` (the live, server-clocked
event stream) and ``vault_op_log_segments`` (archived encrypted
segments per §D14).

This module owns the read-only view: parse + normalize rows from
both sources into :class:`ActivityRow`, sort them by timestamp, and
provide :func:`filter_timeline` for the type-filter + filename-
search controls. The actual GTK ``Gtk.ListView`` lives in
``windows_vault.py`` and consumes whatever this module returns.

Sensitive material never enters an ActivityRow: the row carries a
display-name (sourced from the decrypted manifest cache, not the
ciphertext blob), a timestamp, an event type, and an optional
``device_name`` for the device that emitted the event. The relay's
audit-events table doesn't have plaintext filenames; per §gaps §6
they live in the encrypted op-log only, so the desktop side decodes
them locally with the master key before display.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable


log = logging.getLogger(__name__)


# Whitelisted event-type prefixes the Activity tab knows how to render.
# Anything outside this set lands as ``other``.
ACTIVITY_KIND_PREFIXES = (
    "vault.create",
    "vault.upload",       # uploads + new versions
    "vault.delete",       # soft-delete (T7) + tombstone publish
    "vault.restore",      # restore-version (T7.4) + restore-folder (T11.2)
    "vault.folder",       # folder rename / clear (T4.5 / T14.1)
    "vault.vault",        # whole-vault clear (T14.2)
    "vault.grant",        # T13 device grants
    "vault.revoke",       # T13.5 revocation
    "vault.rotation",     # T13.6 access-secret rotation
    "vault.migration",    # T9 relay migration
    "vault.eviction",     # T7.5 quota eviction
    "vault.purge",        # T14 hard-purge
)


@dataclass
class ActivityRow:
    timestamp_epoch: int
    event_type: str            # the prose anchor — e.g. "vault.upload.completed"
    display_path: str = ""     # plaintext path/folder name (decoded locally)
    device_id: str = ""        # 32-hex-char device id; truncate for UI display
    device_name: str = ""      # human-readable name when available
    summary: str = ""          # short prose suffix for the row
    revision: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def kind_category(self) -> str:
        """First two segments of the event type — drives type-filter checkboxes."""
        parts = self.event_type.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
        return parts[0] if parts else "other"


def normalize_audit_event(row: dict[str, Any]) -> ActivityRow | None:
    """Map a server ``vault_audit_events`` row → :class:`ActivityRow`.

    Ignores rows whose event_type is outside :data:`ACTIVITY_KIND_PREFIXES`
    (returns None), so ad-hoc diagnostic events don't pollute the
    timeline.
    """
    event_type = str(row.get("event_type", "")).strip()
    if not event_type:
        return None
    if not any(event_type.startswith(p) for p in ACTIVITY_KIND_PREFIXES):
        return None
    return ActivityRow(
        timestamp_epoch=int(row.get("created_at", 0)),
        event_type=event_type,
        device_id=str(row.get("device_id") or ""),
        revision=int(row.get("revision") or 0),
        extra=dict(row.get("details") or {}),
    )


def normalize_op_log_entry(entry: dict[str, Any]) -> ActivityRow | None:
    """Map a decrypted op-log entry → :class:`ActivityRow`.

    Op-log entries are decrypted client-side from the encrypted
    segments; their plaintext shape carries display_path + device_name
    + summary, which is what makes them richer than the server's
    audit-events row.
    """
    event_type = str(entry.get("type", "")).strip()
    if not event_type:
        return None
    if not any(event_type.startswith(p) for p in ACTIVITY_KIND_PREFIXES):
        return None
    return ActivityRow(
        timestamp_epoch=int(entry.get("ts", 0)),
        event_type=event_type,
        display_path=str(entry.get("path") or ""),
        device_id=str(entry.get("device_id") or ""),
        device_name=str(entry.get("device_name") or ""),
        summary=str(entry.get("summary") or ""),
        revision=int(entry.get("revision") or 0),
        extra={
            k: v for k, v in entry.items()
            if k not in {"ts", "type", "path", "device_id",
                         "device_name", "summary", "revision"}
        },
    )


def merge_timeline(
    audit_rows: Iterable[dict[str, Any]] | None = None,
    op_log_entries: Iterable[dict[str, Any]] | None = None,
) -> list[ActivityRow]:
    """Combine both sources into a timestamp-sorted timeline.

    Server rows come first (they're cheaper to fetch); op-log entries
    overlay the same events with the richer plaintext when available.
    De-duplication: rows with the same (timestamp, event_type,
    device_id, path) tuple collapse onto whichever has the most fields
    populated.
    """
    out: list[ActivityRow] = []
    if audit_rows:
        for row in audit_rows:
            normalised = normalize_audit_event(row)
            if normalised is not None:
                out.append(normalised)
    if op_log_entries:
        for entry in op_log_entries:
            normalised = normalize_op_log_entry(entry)
            if normalised is not None:
                out.append(normalised)

    # De-duplicate by (timestamp, event_type, device_id, path) — keep
    # the row with the longest summary OR display_path (proxy for
    # "richer plaintext available").
    bucketed: dict[tuple[int, str, str, str], ActivityRow] = {}
    for r in out:
        key = (r.timestamp_epoch, r.event_type, r.device_id, r.display_path)
        prior = bucketed.get(key)
        if prior is None:
            bucketed[key] = r
            continue
        if (
            len(r.summary) + len(r.display_path)
            > len(prior.summary) + len(prior.display_path)
        ):
            bucketed[key] = r

    rows = list(bucketed.values())
    rows.sort(key=lambda r: r.timestamp_epoch, reverse=True)  # newest first
    return rows


def filter_timeline(
    rows: list[ActivityRow],
    *,
    kind_categories: Iterable[str] | None = None,
    filename_search: str | None = None,
) -> list[ActivityRow]:
    """Apply the type-filter + filename-search to a timeline.

    ``kind_categories`` is an iterable of "vault.<topic>" prefixes; if
    None, every row passes. ``filename_search`` is a case-insensitive
    substring; matches against display_path + summary so a search for
    "ledger" catches the "uploaded ledger.txt" row.
    """
    out = rows
    if kind_categories:
        wanted = set(kind_categories)
        out = [r for r in out if r.kind_category in wanted]
    if filename_search:
        needle = filename_search.lower()
        out = [
            r for r in out
            if needle in r.display_path.lower()
            or needle in r.summary.lower()
        ]
    return out


__all__ = [
    "ACTIVITY_KIND_PREFIXES",
    "ActivityRow",
    "filter_timeline",
    "merge_timeline",
    "normalize_audit_event",
    "normalize_op_log_entry",
]
