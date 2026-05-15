"""§3.7 rollback-detection banner for Vault Settings.

Reads :class:`VaultLocalIndex` for the latched rollback flag and
renders an ``Adw.Banner`` above the main vault-settings content.
The banner text is built in a pure function (no GTK imports) so
it stays unit-testable; the widget builder is a thin GTK wrapper
the window calls during ``on_activate``.

Banner clears automatically on the next successful manifest decrypt
(see :meth:`Vault.decrypt_manifest`); the CTA points the user at
the Maintenance tab so they can run the integrity check before
trusting the relay again.
"""

from __future__ import annotations

from typing import Callable


def build_rollback_banner_text(
    served_revision: int,
    floor_revision: int,
) -> str:
    """Compose the persistent-banner copy from the latched record.

    Lead with the concrete numbers so a savvy user can correlate
    with their export bundles' manifest revisions; follow with the
    plain-English what-this-means + the brand-new-device caveat the
    §3.7 risk evaluation calls out explicitly.
    """
    return (
        f"This relay served an older manifest (revision {int(served_revision)}) "
        f"than this device has previously seen (revision {int(floor_revision)}). "
        "Recent changes may be hidden, or the relay may have lost data. "
        "Local state has not been overwritten — run an integrity check, "
        "or restore from a recent export. (A brand-new device cannot "
        "detect rollback before it has seen any state.)"
    )


def build_rollback_banner(
    local_index,
    vault_id: str,
    *,
    on_run_integrity_check: Callable[[], None],
):
    """Return an ``Adw.Banner`` configured for ``vault_id``.

    Returns ``None`` when no rollback is latched — the caller skips
    the append, so unaffected windows get no extra widget in the
    layout tree. Late-imports GTK so the module can be imported in
    non-display contexts (tests, CI).
    """
    record = local_index.get_manifest_rollback(vault_id)
    if record is None:
        return None

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    banner = Adw.Banner.new(
        build_rollback_banner_text(
            served_revision=record["served_revision"],
            floor_revision=record["floor_revision"],
        )
    )
    banner.set_button_label("Run integrity check")
    banner.connect("button-clicked", lambda _b: on_run_integrity_check())
    banner.set_revealed(True)
    return banner
