#!/usr/bin/env python3
"""
Nautilus script: Send selected files to phone via Desktop Connector.
Install to: ~/.local/share/nautilus/scripts/Send to Phone

This script is called by Nautilus with selected file paths in
NAUTILUS_SCRIPT_SELECTED_FILE_PATHS (newline-separated).
"""

import os
import subprocess
import sys

def main():
    # Nautilus/Nemo pass selected files via environment variable
    paths_str = os.environ.get("NAUTILUS_SCRIPT_SELECTED_FILE_PATHS", "")
    if not paths_str.strip():
        paths_str = os.environ.get("NEMO_SCRIPT_SELECTED_FILE_PATHS", "")
    if not paths_str.strip():
        # Fallback: command line arguments (Dolphin, manual use)
        paths = sys.argv[1:]
    else:
        paths = [p for p in paths_str.strip().split("\n") if p]

    if not paths:
        subprocess.run(["notify-send", "-a", "Desktop Connector", "No files selected"])
        return

    files = [p for p in paths if os.path.isfile(p)]
    folders = [p for p in paths if os.path.isdir(p)]

    for path in files:
        subprocess.Popen([
            os.path.expanduser("~/.local/bin/desktop-connector"),
            "--headless", f"--send={path}",
        ])

    if folders:
        word = "folder" if len(folders) == 1 else "folders"
        subprocess.run([
            "notify-send", "-a", "Desktop Connector", "-i", "dialog-warning",
            "Folder transport is not supported",
            f"Skipped {len(folders)} {word}. Send individual files instead.",
        ])

    if files:
        subprocess.run([
            "notify-send", "-a", "Desktop Connector",
            "Sending to phone",
            f"{len(files)} file(s) queued",
        ])

if __name__ == "__main__":
    main()
