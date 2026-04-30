"""
Native system dialogs via zenity (GTK) with tkinter fallback.
"""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_has_zenity = shutil.which("zenity") is not None


def pick_files(title: str = "Select files to send") -> list[Path]:
    """Open a native file picker. Returns list of selected paths."""
    if _has_zenity:
        try:
            result = subprocess.run(
                ["zenity", "--file-selection", "--multiple", "--separator=\n", f"--title={title}"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0 and result.stdout.strip():
                return [Path(p) for p in result.stdout.strip().split("\n") if p]
        except Exception as e:
            log.warning("platform.dialog.failed kind=file_picker error_kind=%s", type(e).__name__)
        return []

    # Fallback to tkinter
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    paths = filedialog.askopenfilenames(title=title)
    root.destroy()
    return [Path(p) for p in paths] if paths else []


def save_file(
    title: str,
    *,
    default_filename: str = "",
    file_types: tuple[tuple[str, str], ...] = (),
) -> Path | None:
    """Open a native save-file picker. Returns the chosen path or None.

    ``file_types`` is a tuple of ``(label, glob)`` pairs forwarded to
    zenity's ``--file-filter``. The first entry shows by default.
    """
    if _has_zenity:
        try:
            cmd = [
                "zenity", "--file-selection", "--save",
                "--confirm-overwrite",
                f"--title={title}",
            ]
            if default_filename:
                cmd.append(f"--filename={default_filename}")
            for label, glob in file_types:
                cmd.append(f"--file-filter={label} | {glob}")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0 and result.stdout.strip():
                return Path(result.stdout.strip())
        except Exception as e:
            log.warning(
                "platform.dialog.failed kind=save_file error_kind=%s",
                type(e).__name__,
            )
        return None

    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    chosen = filedialog.asksaveasfilename(
        title=title,
        initialfile=default_filename,
        filetypes=list(file_types),
    )
    root.destroy()
    return Path(chosen) if chosen else None


def confirm(title: str, message: str) -> bool:
    """Show a confirmation dialog. Returns True if confirmed."""
    if _has_zenity:
        try:
            result = subprocess.run(
                ["zenity", "--question", f"--title={title}", f"--text={message}",
                 "--width=350"],
                timeout=120,
            )
            return result.returncode == 0
        except Exception as e:
            log.warning("platform.dialog.failed kind=confirm error_kind=%s", type(e).__name__)
        return False

    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    answer = messagebox.askyesno(title, message, parent=root)
    root.destroy()
    return answer


def show_info(title: str, message: str) -> None:
    """Show an info dialog."""
    if _has_zenity:
        try:
            subprocess.run(
                ["zenity", "--info", f"--title={title}", f"--text={message}", "--width=300"],
                timeout=60,
            )
        except Exception:
            pass
        return

    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(title, message, parent=root)
    root.destroy()
