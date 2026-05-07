"""Liveness probes: device stats + ping/pong."""


class LivenessMixin:
    def get_stats(self, paired_with: str | None = None) -> dict | None:
        """Get connection statistics from the server."""
        path = "/api/devices/stats"
        if paired_with:
            path += f"?paired_with={paired_with}"
        resp = self.conn.request("GET", path)
        if resp and resp.status_code == 200:
            return resp.json()
        return None

    def ping_device(self, recipient_id: str, timeout: float = 8.0) -> dict | None:
        """Probe paired device liveness. Server sends HIGH FCM and waits up to 5s
        for pong. Returns {online, last_seen_at, rtt_ms, via} or None on failure."""
        resp = self.conn.request(
            "POST", "/api/devices/ping",
            json={"recipient_id": recipient_id},
            timeout=timeout,
        )
        if resp and resp.status_code == 200:
            return resp.json()
        return None
