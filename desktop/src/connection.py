"""
Connection manager with exponential backoff and retry logic.
"""

import enum
import logging
import random
import threading
import time

import requests

log = logging.getLogger(__name__)

INITIAL_BACKOFF = 2.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF = 300.0
JITTER = 0.2


class ConnectionState(enum.Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


class ConnectionManager:
    """Manages connection state with exponential backoff."""

    def __init__(self, server_url: str, device_id: str, auth_token: str):
        self.server_url = server_url.rstrip("/")
        self.device_id = device_id
        self.auth_token = auth_token

        self._state = ConnectionState.DISCONNECTED
        self._retry_count = 0
        self._current_backoff = INITIAL_BACKOFF
        self._next_retry_at = 0.0
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._callbacks: list = []

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def retry_count(self) -> int:
        with self._lock:
            return self._retry_count

    @property
    def current_backoff(self) -> float:
        with self._lock:
            return self._current_backoff

    @property
    def seconds_until_retry(self) -> float:
        with self._lock:
            return max(0.0, self._next_retry_at - time.time())

    def on_state_change(self, callback) -> None:
        self._callbacks.append(callback)

    def _set_state(self, new_state: ConnectionState) -> None:
        with self._lock:
            old = self._state
            self._state = new_state
        if old != new_state:
            for cb in self._callbacks:
                try:
                    cb(new_state)
                except Exception:
                    log.exception("State change callback error")

    def auth_headers(self) -> dict:
        return {
            "X-Device-ID": self.device_id,
            "Authorization": f"Bearer {self.auth_token}",
        }

    def check_connection(self) -> bool:
        """Ping the health endpoint. Returns True if server is reachable."""
        self._set_state(ConnectionState.RECONNECTING)
        try:
            resp = requests.get(
                f"{self.server_url}/api/health",
                headers=self.auth_headers(),
                timeout=3,
            )
            if resp.status_code == 200:
                self._on_success()
                return True
        except requests.RequestException:
            pass
        self._on_failure()
        return False

    def _on_success(self) -> None:
        with self._lock:
            self._retry_count = 0
            self._current_backoff = INITIAL_BACKOFF
        self._set_state(ConnectionState.CONNECTED)

    def _on_failure(self) -> None:
        with self._lock:
            self._retry_count += 1
            raw = min(INITIAL_BACKOFF * (BACKOFF_MULTIPLIER ** (self._retry_count - 1)), MAX_BACKOFF)
            jitter_range = raw * JITTER
            self._current_backoff = raw + random.uniform(-jitter_range, jitter_range)
            self._next_retry_at = time.time() + self._current_backoff
        self._set_state(ConnectionState.DISCONNECTED)
        with self._lock:
            log.info(
                "Connection failed (attempt #%d). Next retry in %.1fs",
                self._retry_count, self._current_backoff,
            )

    def wait_for_retry(self) -> bool:
        """Sleep until next retry time. Returns False if interrupted by try_now()."""
        wait_time = self.seconds_until_retry
        if wait_time > 0:
            self._wake_event.clear()
            return not self._wake_event.wait(timeout=wait_time)
        return True

    def try_now(self) -> None:
        """Manual retry — reset backoff and attempt immediately."""
        with self._lock:
            self._retry_count = 0
            self._current_backoff = INITIAL_BACKOFF
            self._next_retry_at = 0
        self._wake_event.set()
        log.info("Manual retry triggered")

    def get_status_text(self) -> str:
        state = self.state
        if state == ConnectionState.CONNECTED:
            return "Connected"
        elif state == ConnectionState.RECONNECTING:
            return "Connecting..."
        else:
            with self._lock:
                retry = self._retry_count
                secs = max(0, int(self._next_retry_at - time.time()))
            return f"Offline - retry #{retry} in {secs}s"

    def request(self, method: str, path: str, **kwargs) -> requests.Response | None:
        """
        Make an authenticated request. Returns response on success,
        None on connection failure (and updates backoff state).
        """
        url = f"{self.server_url}{path}"
        kwargs.setdefault("headers", {}).update(self.auth_headers())
        kwargs.setdefault("timeout", 30)
        try:
            resp = requests.request(method, url, **kwargs)
            self._on_success()
            return resp
        except requests.RequestException as e:
            log.warning("Request failed: %s %s: %s", method, path, e)
            self._on_failure()
            return None
