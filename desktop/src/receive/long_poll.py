"""Long-poll detection + execution + status-file plumbing.

Uses raw requests so a slow/broken /notify endpoint does NOT bump the
ConnectionManager state machine — only the short health-check probe
in ConnectionManager affects connection state.
"""

import logging

import requests as _raw_requests

log = logging.getLogger(__name__)


class LongPollMixin:
    def _write_poll_status(self, status: str) -> None:
        """Write long poll status: 'active', 'unavailable', 'testing', 'offline'."""
        try:
            import json
            self._poll_status_file.write_text(json.dumps({"long_poll": status}))
        except Exception:
            pass

    def _test_long_poll(self) -> bool:
        """Quick test if /notify endpoint exists. Returns True if available."""
        try:
            resp = _raw_requests.get(
                f"{self.conn.server_url}/api/transfers/notify?test=1",
                headers=self.conn.auth_headers(), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def retry_long_poll(self) -> None:
        """Reset long poll state to re-test on next cycle."""
        self._write_poll_status("testing")
        self._wake_event.set()

    def _long_poll(self, since: int) -> dict | bool | None:
        """
        Long poll the server. Returns:
        - dict: response data (something happened)
        - False: timed out, nothing new
        - None: endpoint not available or error (fall back to regular polling)
        Uses raw requests — does NOT affect connection state machine.
        """
        url = f"{self.conn.server_url}/api/transfers/notify?since={since}"
        try:
            resp = _raw_requests.get(url, headers=self.conn.auth_headers(), timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("pending") or data.get("delivered") or data.get("download_progress"):
                    return data
                return False
            return None
        except _raw_requests.RequestException:
            return None
