"""Tuning knobs shared across the upload modules."""

from typing import Literal

CHUNK_SIZE = 2 * 1024 * 1024  # 2 MiB; must match the download-side reader
MAX_FILE_BYTES_DEFAULT = 2 * 1024 * 1024 * 1024  # §gaps §7 per-file cap (2 GiB)
CAS_MAX_RETRIES = 5

UploadMode = Literal["new_file_or_version", "new_file_only", "append_version_only"]
