"""ApiClient: thin composition of route mixins.

Holds the only persistent state (``self.conn``, ``self.crypto``,
optional ``self._capabilities_cache``). All HTTP behaviour lives in
the topical mixins; this class only wires them together so callers
can keep the single ``ApiClient`` surface unchanged.
"""

from ..connection import ConnectionManager
from ..crypto import KeyManager
from .capabilities import CapabilitiesMixin
from .fasttrack import FasttrackMixin
from .liveness import LivenessMixin
from .pairing import PairingMixin
from .registration import RegistrationMixin
from .transfers_chunks import TransfersChunksMixin
from .transfers_init import TransfersInitMixin
from .transfers_lifecycle import TransfersLifecycleMixin
from .transfers_send import TransfersSendMixin
from .transfers_streaming import TransfersStreamingMixin


class ApiClient(
    RegistrationMixin,
    PairingMixin,
    TransfersInitMixin,
    TransfersChunksMixin,
    TransfersStreamingMixin,
    TransfersLifecycleMixin,
    TransfersSendMixin,
    FasttrackMixin,
    LivenessMixin,
    CapabilitiesMixin,
):
    """High-level API client wrapping ConnectionManager for server operations."""

    def __init__(self, connection: ConnectionManager, crypto: KeyManager):
        self.conn = connection
        self.crypto = crypto
