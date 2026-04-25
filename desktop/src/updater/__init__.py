"""In-app updater for the AppImage release shape (P.6).

Two halves:
  - :mod:`.version_check` — module-level polling of GitHub Releases
    against the locally bundled version.json.
  - :mod:`.update_runner` — invokes AppImageUpdate against $APPIMAGE
    (lands in P.6b).

Both halves no-op outside an AppImage ($APPIMAGE unset) so apt-pip
and dev-tree installs see no update plumbing — they can't act on an
update via AppImageUpdate anyway.
"""
