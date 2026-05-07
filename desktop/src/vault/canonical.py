"""Canonical-JSON + RFC 3339 timestamp helpers shared across the vault paths."""

import datetime
import json


def _canonical_json(obj: dict) -> bytes:
    """v1 stdlib-canonical JSON (formats §17): sorted keys, `(,:)`
    separators, default ASCII-only `\\uXXXX` escaping for non-ASCII.
    Strict subset of RFC 8785; chosen so the desktop and a PHP/etc.
    re-implementer can round-trip without an external library. v1
    plaintext uses no floats — see formats §17."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now_rfc3339() -> str:
    """RFC 3339 ms-precision UTC timestamp."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
