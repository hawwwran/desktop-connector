"""Atomic temp → unique-name finalize for received files.

Used by both classic and streaming download paths after a chunked
write to ``.parts/.incoming_*.part`` completes. ``os.link`` is the
fast/atomic path; cross-FS volumes (FAT, exFAT, /tmp on tmpfs cross-FS
to home) fall through to ``shutil.move`` with the small unique-name
race accepted.
"""

import errno
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


class FinalizeMixin:
    @classmethod
    def _finalize_temp_to_unique(cls, temp_path: Path, save_dir: Path,
                                 filename: str) -> Path | None:
        """Atomically link temp_path under save_dir using the first
        non-colliding name (filename, filename_1, ...), then unlink the
        temp source. os.link is atomic and FileExistsError-safe, so no
        TOCTOU race with another writer claiming the same name.

        Falls back to a probe + shutil.move on cross-FS or when the FS
        does not support hard links (FAT, exFAT). The fallback retains
        the small unique-name race, accepted as the degenerate case."""
        base = save_dir / filename
        stem = base.stem
        suffix = base.suffix
        counter = 0
        while True:
            candidate = base if counter == 0 else save_dir / f"{stem}_{counter}{suffix}"
            try:
                os.link(temp_path, candidate)
            except FileExistsError:
                counter += 1
                continue
            except OSError as e:
                if e.errno in (errno.EXDEV, errno.EPERM, errno.ENOSYS):
                    return cls._fallback_move_unique(temp_path, save_dir, filename)
                log.exception("os.link finalize failed for %s", temp_path)
                cls._delete_quietly(temp_path)
                return None
            cls._delete_quietly(temp_path)
            return candidate

    @classmethod
    def _fallback_move_unique(cls, temp_path: Path, save_dir: Path,
                              filename: str) -> Path | None:
        """Cross-FS finalize fallback: probe for a free name, then move.
        Small TOCTOU race accepted (cross-FS deployments are rare and
        single-user)."""
        base = save_dir / filename
        stem = base.stem
        suffix = base.suffix
        counter = 0
        while True:
            candidate = base if counter == 0 else save_dir / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                break
            counter += 1
        try:
            shutil.move(str(temp_path), str(candidate))
            return candidate
        except OSError:
            log.exception("Cross-FS finalize failed for %s", temp_path)
            cls._delete_quietly(temp_path)
            return None

    @staticmethod
    def _delete_quietly(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Failed to delete %s", path, exc_info=True)
