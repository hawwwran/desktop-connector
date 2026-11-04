"""Transfer History window (`python -m src.windows history`).

Pre-split this lived as a single ~1000-line ``windows_history.py``
with one giant ``show_history`` function whose nested ``on_activate``
in turn nested ``_selected_device_id``, ``_compute_status``,
``_create_row``, ``_update_row``, ``build_list``, ``refresh_tick``
and the rest of the per-row + abort-flow + zombie-scrub helpers. The
package keeps the same public import surface (callers just need
``show_history``); internals are split per-cohesion across the
sibling modules.
"""

from .window import show_history

__all__ = ["show_history"]
