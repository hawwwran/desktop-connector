"""Back-compat shim for the receive-side poller.

The implementation lives under ``desktop/src/receive/`` (composed from
topical mixins). This module re-exports the public surface so legacy
``from .poller import …`` imports keep working unchanged.

External callers should prefer ``from .receive import …`` going forward;
this shim will be removed once all in-tree imports have migrated.

Note: ``time`` is re-imported here so tests that
``patch.object(poller_mod.time, "sleep")`` continue to take effect —
the ``time`` module is a stdlib singleton, so a patch via any
reference reaches the actual call sites in the receive mixins.
"""

import time  # noqa: F401  — re-exported for test patches

from .receive import (
    CHUNK_DOWNLOAD_ATTEMPTS,
    DELIVERY_STALL_TIMEOUT,
    FASTTRACK_POLL_INTERVAL_S,
    Poller,
    STALE_PART_TTL_S,
    STREAM_CHUNK_NETWORK_ATTEMPTS,
    STREAM_CHUNK_WAIT_BUDGET_S,
    STREAM_CHUNK_WAIT_RAMP_S,
)

__all__ = [
    "Poller",
    "CHUNK_DOWNLOAD_ATTEMPTS",
    "DELIVERY_STALL_TIMEOUT",
    "FASTTRACK_POLL_INTERVAL_S",
    "STALE_PART_TTL_S",
    "STREAM_CHUNK_NETWORK_ATTEMPTS",
    "STREAM_CHUNK_WAIT_BUDGET_S",
    "STREAM_CHUNK_WAIT_RAMP_S",
]
