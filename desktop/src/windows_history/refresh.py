"""History list build + adaptive refresh tick.

``build_list`` is the diff-driven list painter. It computes a
structural signature (per-row identity + direction) and a progress
signature (per-row mutable fields) and short-circuits if the progress
signature hasn't changed, only does in-place updates if the structural
signature still matches, and falls back to a per-row insert/remove
diff otherwise. ``refresh_tick`` self-reschedules at 1 s while a
transfer is active or 3 s when idle.

``_scrub_zombie_waiting`` is called at the top of every ``build_list``
tick so rows age from Waiting → Failed without the user needing to
close + reopen — see CLAUDE.md "WAITING + zombie scrub".
"""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from .context import HistoryContext
from .device_filter import (
    _empty_history_text,
    _selected_device_id,
)
from .rows import create_row as _create_row, update_row as _update_row
from .status import row_key as _row_key
from .zombie_scrub import scrub_zombie_waiting as _scrub_zombie_waiting


def build_list(ctx: HistoryContext) -> bool:
    history = ctx.history
    list_container = ctx.list_container
    row_widgets = ctx.row_widgets
    all_widgets = ctx.all_widgets
    structural_sig = ctx.structural_sig
    progress_sig = ctx.progress_sig
    empty_label = ctx.empty_label
    has_active = ctx.has_active

    _scrub_zombie_waiting(ctx)
    history._items = history._load()
    selected_id = _selected_device_id(ctx)
    items = (
        history.items_for_peer(
            selected_id,
            fallback_device_id=selected_id,
        )
        if selected_id else []
    )

    # Structural sig: item identity and base state
    s_sig = (
        selected_id,
        tuple((_row_key(i), i.get("direction")) for i in items),
    )
    # Progress sig: all mutable fields
    p_sig = (
        selected_id,
        tuple(
            (_row_key(i), i.get("status"), i.get("delivered"),
             i.get("chunks_downloaded", 0),
             i.get("recipient_chunks_downloaded", 0))
            for i in items
        ),
    )

    if p_sig == progress_sig[0]:
        return True  # Nothing changed at all

    # Check if we need a full rebuild or just in-place updates
    needs_rebuild = (s_sig != structural_sig[0])
    structural_sig[0] = s_sig
    progress_sig[0] = p_sig

    # Update has_active flag
    downloading = any(i.get("status") in ("downloading", "uploading") for i in items)
    active_sent = any(
        i.get("direction") == "sent" and not i.get("delivered")
        for i in items
    )
    has_active[0] = downloading or active_sent

    if needs_rebuild:
        # Diff instead of full rebuild so adding one item or
        # deleting one row doesn't tear down and recreate every
        # other card (O(N) widget churn on every add/remove used
        # to cause visible jank on a full 50-item history).
        #
        # Also critical for the delete animation: a row in the
        # .removing transition window must not be ripped out of
        # the tree by an interleaving refresh — the 300 ms
        # GLib.timeout_add in on_delete handles final removal.
        new_tids_ordered = [_row_key(i) for i in items]
        by_tid_item = dict(zip(new_tids_ordered, items))

        current_tids = list(row_widgets.keys())
        new_tids_set = set(new_tids_ordered)
        removed_tids = [t for t in current_tids if t not in new_tids_set]

        # Drop the empty-state label if we now have items.
        if items and empty_label[0] is not None:
            list_container.remove(empty_label[0])
            if empty_label[0] in all_widgets:
                all_widgets.remove(empty_label[0])
            empty_label[0] = None

        # Remove rows whose tid is gone. Skip widgets already
        # animating out via the delete button — the GLib timer
        # there will finalize them.
        for tid in removed_tids:
            entry = row_widgets.get(tid)
            if not entry:
                continue
            card, _row, _pbar = entry
            if "removing" in card.get_css_classes():
                continue
            try:
                list_container.remove(card)
            except Exception:
                pass
            if card in all_widgets:
                all_widgets.remove(card)
            row_widgets.pop(tid, None)

        # Insert newcomers at their correct positions. Assumes
        # `items` is ordered consistently across ticks (history
        # preserves insertion order, newest first).
        for idx, tid in enumerate(new_tids_ordered):
            if tid in row_widgets:
                continue
            item = by_tid_item[tid]
            card, row, pbar = _create_row(ctx, item)
            if idx == 0:
                list_container.prepend(card)
            else:
                prev_tid = new_tids_ordered[idx - 1]
                prev_entry = row_widgets.get(prev_tid)
                if prev_entry is not None:
                    list_container.insert_child_after(card, prev_entry[0])
                else:
                    # Previous row isn't in the tree yet (unlikely
                    # with newest-first order + prepend loop) —
                    # append as a safe fallback.
                    list_container.append(card)
            row_widgets[tid] = (card, row, pbar)
            all_widgets.insert(min(idx, len(all_widgets)), card)

        # Empty-state label if nothing remains.
        if not items:
            if empty_label[0] is None:
                empty = Gtk.Label(label=_empty_history_text(ctx))
                empty.add_css_class("dim-label")
                empty.set_margin_top(48)
                empty.set_margin_bottom(48)
                list_container.append(empty)
                all_widgets.append(empty)
                empty_label[0] = empty
            else:
                empty_label[0].set_text(_empty_history_text(ctx))

    # In-place update on every tick — refresh subtitles and
    # progress bars for rows that existed before AND for rows we
    # just added (cheap, idempotent, makes the code branch-free).
    for item in items:
        tid = _row_key(item)
        entry = row_widgets.get(tid)
        if entry:
            box, row, old_pbar = entry
            new_pbar = _update_row(item, row, old_pbar, box)
            row_widgets[tid] = (box, row, new_pbar)

    return True


def refresh_tick(ctx: HistoryContext) -> bool:
    # Adaptive refresh: 1s during active transfers, 3s otherwise
    ctx.build_list()
    interval = 1000 if ctx.has_active[0] else 3000
    GLib.timeout_add(interval, ctx.refresh_tick)
    return False  # don't repeat this one, the new timeout takes over
