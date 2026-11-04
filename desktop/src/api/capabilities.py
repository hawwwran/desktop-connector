"""Server capability probe + FCM availability check.

Both use unauthenticated GETs so a broken auth token doesn't prevent
clients from discovering the server's feature flags. Capability result
is cached for 60 s; FCM probe is uncached (only called on send paths
that already have a result-caching layer above).
"""

import time

import requests

from .constants import CAPABILITY_CACHE_TTL_S, CAPABILITY_STREAM_V1


class CapabilitiesMixin:
    def get_capabilities(self, *, force_refresh: bool = False) -> set[str]:
        """Probe GET /api/health for the server's advertised capabilities.

        Returns the set of capability tokens (e.g. ``{"stream_v1"}``).
        Old servers that don't advertise a `capabilities` field return
        the empty set, which is how clients discover streaming is not
        available.

        Result is cached for ``CAPABILITY_CACHE_TTL_S`` (60 s) so the
        streaming sender / receiver can check cheaply per-transfer
        without hammering /api/health. Pass ``force_refresh=True`` to
        bypass the cache (e.g. after a server reconfigure).

        Uses an unauthenticated request so a broken auth token doesn't
        prevent clients from discovering that streaming is unavailable.
        """
        now = time.monotonic()
        cached = getattr(self, "_capabilities_cache", None)
        if not force_refresh and cached is not None:
            caps, expires_at = cached
            if expires_at > now:
                return caps
        caps: set[str] = set()
        try:
            resp = requests.get(
                f"{self.conn.server_url}/api/health",
                timeout=5,
            )
            if resp.status_code == 200:
                body = resp.json()
                raw = body.get("capabilities") if isinstance(body, dict) else None
                if isinstance(raw, list):
                    caps = {c for c in raw if isinstance(c, str)}
        except (requests.RequestException, ValueError):
            # Treat probe failure as "no known capabilities" but do NOT
            # cache the empty result — a transient network blip should
            # not pin us to classic for the next minute.
            return set()
        self._capabilities_cache = (caps, now + CAPABILITY_CACHE_TTL_S)
        return caps

    def supports_streaming(self) -> bool:
        """Convenience shortcut: ``CAPABILITY_STREAM_V1 in get_capabilities()``.

        Streaming senders gate on this before requesting
        ``mode="streaming"`` at init. A False return forces the classic
        path — both for genuinely-old servers and for deployments where
        the operator turned ``streamingEnabled`` off in server config.
        """
        return CAPABILITY_STREAM_V1 in self.get_capabilities()

    def check_fcm_available(self) -> bool:
        """Check if the server has FCM configured. Uses unauthenticated endpoint."""
        try:
            resp = requests.get(
                f"{self.conn.server_url}/api/fcm/config",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("available", False)
        except requests.RequestException:
            pass
        return False
