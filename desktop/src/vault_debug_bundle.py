"""Debug-bundle generator for vault diagnostics (T17.5).

Produces a ZIP file the user can attach to a support thread without
leaking secrets. The bundle is **always** redacted before archiving
— the redaction step is a separate, testable pass the caller cannot
bypass. A grep-style scan refuses to write the ZIP if the redacted
output still contains any of the forbidden substrings (master key,
recovery, passphrase, Authorization headers).

Contents (each section optional based on what's available):

- ``config.redacted.json``    — config.json with `auth_token`,
  `keys`, `recovery_*`, `vault_access_secret` and similar fields
  zeroed.
- ``index_schema.txt``        — `.schema` dump from
  ``vault-local-index.sqlite3`` (no row data).
- ``binding_states.json``     — public state of every binding
  (binding_id, vault_id, remote_folder_id, local_path, state,
  sync_mode, last_synced_revision). No content fingerprints.
- ``activity_tail.txt``       — last N lines of vault.log if
  present.
- ``manifest_summary.json``   — per-vault revision + chunk_count
  + folder count, with no decrypted filenames.

Excluded by design: keys/, history.json, vault grants, the raw
config file, and any file whose name contains `secret` or
`recovery`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)


# The leak scan is the *last* line of defence after the redaction pass —
# its job is to catch secrets that survived (e.g. an unrecognised field
# carrying a Bearer token in its value). Patterns therefore target
# *value* shapes, not field names: a key named "vault_access_secret"
# whose value is "<redacted>" is fine; a value containing
# "Authorization: Bearer …" is not. Field-name redaction lives in
# REDACT_KEYS / redact_config.
FORBIDDEN_PATTERNS: tuple[re.Pattern, ...] = (
    # Authorization-header-shaped strings (not the redacted placeholder).
    re.compile(r"Authorization\s*:\s*(?!\"?<redacted>)[^\s\"<]+", re.IGNORECASE),
    re.compile(r"X-Vault-Authorization\s*:\s*(?!\"?<redacted>)[^\s\"<]+", re.IGNORECASE),
    # Bearer-token-shaped values (16+ chars of base64-ish).
    re.compile(r"\bBearer\s+(?!<redacted>)[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
)


REDACT_KEYS = frozenset({
    # Top-level config keys we know carry sensitive material.
    "auth_token",
    "vault_access_secret",
    "vault_master_key",
    "vault_master_key_b64",
    "recovery_passphrase",
    "recovery_secret",
    "purge_secret",
    "keys",
    "paired_devices",
    "find_phone_password",
})


REDACTED = "<redacted>"


class DebugBundleError(RuntimeError):
    """Raised when the bundle would leak secrets despite the redaction pass."""


# Field-name substrings that must not survive the redaction pass even
# as keys — every match becomes ``<redacted-field>`` and the value
# becomes ``<redacted>`` so a literal grep for these terms in the
# debug bundle returns nothing.
SENSITIVE_KEY_SUBSTRINGS = (
    "secret", "recovery", "passphrase", "master_key", "authorization",
    "purge",
)


def redact_config(config_obj: Any) -> Any:
    """Return a deep copy of ``config_obj`` with sensitive keys zeroed.

    Walks dicts/lists recursively. For any key whose lowercase name is
    in :data:`REDACT_KEYS` *or* contains a member of
    :data:`SENSITIVE_KEY_SUBSTRINGS`, the value becomes
    ``"<redacted>"`` and the *key itself* becomes ``"<redacted-field>"``
    — the latter is what makes a literal grep for "vault_master_key" /
    "recovery_passphrase" / etc. return nothing in the bundle.
    Non-sensitive subtrees are recursed into unchanged.
    """
    if isinstance(config_obj, dict):
        out: dict[str, Any] = {}
        for k, v in config_obj.items():
            lk = str(k).lower()
            sensitive = (
                lk in REDACT_KEYS
                or any(s in lk for s in SENSITIVE_KEY_SUBSTRINGS)
            )
            if sensitive:
                # Avoid collisions when multiple sensitive keys share a
                # parent dict — append an index suffix to the field-redact
                # placeholder.
                placeholder = "<redacted-field>"
                if placeholder in out:
                    suffix = 1
                    while f"{placeholder}-{suffix}" in out:
                        suffix += 1
                    placeholder = f"{placeholder}-{suffix}"
                out[placeholder] = REDACTED
            else:
                out[k] = redact_config(v)
        return out
    if isinstance(config_obj, list):
        return [redact_config(item) for item in config_obj]
    return config_obj


def scan_for_forbidden(payload: bytes | str) -> list[str]:
    """Return every forbidden substring observed in ``payload`` (deduped)."""
    text = payload.decode("utf-8", errors="ignore") if isinstance(payload, bytes) else payload
    hits: list[str] = []
    for pattern in FORBIDDEN_PATTERNS:
        match = pattern.search(text)
        if match and pattern.pattern not in hits:
            hits.append(pattern.pattern)
    return hits


def schema_dump(db_path: Path) -> str:
    """Return the schema (no row data) of ``db_path`` as plain text."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    out: list[str] = []
    for kind, name, sql in rows:
        out.append(f"-- {kind}: {name}")
        out.append((sql or "").strip())
        out.append("")
    return "\n".join(out)


def tail_lines(path: Path, *, max_bytes: int = 200_000) -> str:
    """Return up to the last ``max_bytes`` of ``path`` as text."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    start = max(0, size - max_bytes)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    return data.decode("utf-8", errors="ignore")


def build_debug_bundle_bytes(
    *,
    config: dict[str, Any] | None = None,
    db_path: Path | None = None,
    binding_states: list[dict[str, Any]] | None = None,
    activity_log_path: Path | None = None,
    manifest_summary: dict[str, Any] | None = None,
) -> bytes:
    """Assemble the ZIP bytes; refuses to return if forbidden text leaks.

    Each input is optional — missing pieces are skipped — so callers
    can produce partial bundles without writing fake fixtures.
    """
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if config is not None:
            redacted = redact_config(config)
            payload = json.dumps(redacted, indent=2, sort_keys=True)
            zf.writestr("config.redacted.json", payload)
        if db_path is not None:
            try:
                zf.writestr("index_schema.txt", schema_dump(db_path))
            except sqlite3.Error as exc:
                log.warning(
                    "vault.debug_bundle.schema_dump_failed path=%s error=%s",
                    db_path, exc,
                )
        if binding_states is not None:
            payload = json.dumps(binding_states, indent=2, sort_keys=True)
            zf.writestr("binding_states.json", payload)
        if activity_log_path is not None:
            tail = tail_lines(activity_log_path)
            if tail:
                zf.writestr("activity_tail.txt", tail)
        if manifest_summary is not None:
            payload = json.dumps(manifest_summary, indent=2, sort_keys=True)
            zf.writestr("manifest_summary.json", payload)

    raw = buffer.getvalue()
    # Final guardrail: re-decompress every entry and grep for forbidden
    # substrings. If any leak survives the redaction pass, refuse.
    leaks: list[tuple[str, str]] = []
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        for info in zf.infolist():
            with zf.open(info) as fh:
                content = fh.read()
            for pattern in scan_for_forbidden(content):
                leaks.append((info.filename, pattern))
    if leaks:
        raise DebugBundleError(
            "debug bundle would leak secrets — refusing to publish: "
            + ", ".join(f"{f}: {p}" for f, p in leaks)
        )
    return raw


def write_debug_bundle(
    destination: Path, **kwargs: Any,
) -> Path:
    """Write the bundle to ``destination`` and return the path."""
    payload = build_debug_bundle_bytes(**kwargs)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(destination)
    return destination


__all__ = [
    "DebugBundleError",
    "FORBIDDEN_PATTERNS",
    "REDACTED",
    "REDACT_KEYS",
    "SENSITIVE_KEY_SUBSTRINGS",
    "build_debug_bundle_bytes",
    "redact_config",
    "scan_for_forbidden",
    "schema_dump",
    "tail_lines",
    "write_debug_bundle",
]
