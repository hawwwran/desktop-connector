"""Free helpers shared by the single-file and folder upload paths."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path


def _hash_file(local_path: Path) -> tuple[bytes, int]:
    """Stream a SHA-256 over the file; return (digest, byte length)."""
    h = hashlib.sha256()
    total = 0
    with open(local_path, "rb") as fh:
        while True:
            chunk = fh.read(1 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.digest(), total


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
