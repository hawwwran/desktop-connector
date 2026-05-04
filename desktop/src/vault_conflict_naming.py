"""§A20 conflict-rename helper (T11.3).

Three callers in the codebase need to produce a "conflict copy" name
when a file would otherwise overwrite a live local entry:

- **Browser-side upload "Keep both"** (T6.2) — the user picked Keep
  both at the conflict prompt; the imported bytes land at a new path
  derived from the colliding one.
- **Bundle import** (T8) — a `.dcvault` bundle's file collides with
  an active manifest entry and the user chose "rename" resolution.
- **Restore / two-way sync** (T11.2 / T12) — restoring or syncing a
  remote file would overwrite a different local file at the same
  path; the local copy is preserved by being renamed (the restore
  proceeds at the original path).

All three want the same surface form so the user's file manager
shows a uniform vocabulary regardless of which path produced the
conflict copy:

    <stem> (conflict <kind>[ <device-name>] <YYYY-MM-DD HH-MM>)<ext>

- ``kind`` is the verb: "uploaded", "imported", "synced", "restored".
- ``device-name`` is shown only when known (import has no device-name
  context, so it is omitted there).
- ``when`` is rendered in UTC short form (`%Y-%m-%d %H-%M`).
- The suffix is *appended* to the leaf — chained conflicts on a
  pre-existing renamed path stack rather than rewriting (matches the
  §A20 "Recursion" example).
- The directory portion is preserved verbatim so the conflict copy
  lands in the same folder as the original.

The previous T6.2 / T8 helpers (`make_conflict_renamed_path` /
`_conflict_imported_path`) now delegate here, so all three callers
produce byte-identical names for the same inputs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


KNOWN_KINDS = frozenset({"uploaded", "imported", "synced", "restored"})

# Filesystem-unsafe characters in the device-name field — replace
# rather than reject so a quirky hostname doesn't crash the rename.
_DEVICE_NAME_SAFE = set("._- ")


def make_conflict_path(
    *,
    original_path: str,
    kind: str,
    when: datetime | None = None,
    device_name: str | None = None,
) -> str:
    """Return the §A20 conflict-copy path for ``original_path``.

    ``kind`` controls the verb in the suffix; ``device_name`` (when
    provided) is sanitised to filesystem-safe characters and inserted
    between the verb and the timestamp.
    """
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("kind must be a non-empty string")

    raw = str(original_path).replace("\\", "/")
    parts = [p for p in raw.split("/") if p]
    if not parts:
        raise ValueError("original_path is empty")
    leaf = parts[-1]
    parent = "/".join(parts[:-1])

    dot = leaf.rfind(".")
    if dot > 0:
        stem = leaf[:dot]
        ext = leaf[dot:]
    else:
        stem = leaf
        ext = ""

    timestamp = (when or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).strftime("%Y-%m-%d %H-%M")
    pieces = ["conflict", kind.strip()]
    if device_name is not None:
        sanitized = _sanitize_device_name(device_name)
        if sanitized:
            pieces.append(sanitized)
    pieces.append(timestamp)
    suffix = " (" + " ".join(pieces) + ")"
    new_leaf = f"{stem}{suffix}{ext}"
    return f"{parent}/{new_leaf}" if parent else new_leaf


def _sanitize_device_name(device_name: str) -> str:
    candidate = "".join(
        ch if (ch.isalnum() or ch in _DEVICE_NAME_SAFE) else "_"
        for ch in str(device_name).strip()
    ).strip()
    return candidate or "device"


def short_timestamp(when: datetime | str) -> str:
    """Render a ``datetime`` (or RFC3339 string) as ``YYYY-MM-DD HH-MM``."""
    if isinstance(when, str):
        normalized = when.replace("Z", "+00:00") if when.endswith("Z") else when
        try:
            when = datetime.fromisoformat(normalized)
        except ValueError:
            return when  # passthrough so callers can fall back gracefully
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H-%M")


__all__ = [
    "KNOWN_KINDS",
    "make_conflict_path",
    "short_timestamp",
]
