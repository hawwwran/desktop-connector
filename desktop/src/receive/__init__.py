"""Receive-side composition.

``Poller`` (the orchestrator) lives in ``poller.py``; per-topic
behaviour is in mixins under this package. The legacy
``desktop/src/poller.py`` remains a back-compat shim re-exporting
``Poller`` plus the module-level constants tests / consumers reach for.
"""

from .classic_download import CHUNK_DOWNLOAD_ATTEMPTS, STALE_PART_TTL_S
from .delivery_tracker import DELIVERY_STALL_TIMEOUT
from .fasttrack import FASTTRACK_POLL_INTERVAL_S
from .poller import Poller
from .streaming_download import (
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
