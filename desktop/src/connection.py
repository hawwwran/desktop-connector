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


class AuthFailureKind(enum.Enum):
    CREDENTIALS_INVALID = "credentials_invalid"  # 401 — device_id/auth_token don't match the server
    PAIRING_MISSING = "pairing_missing"           # 403 — authed, but no pairings row links this pair


# Persistent 401/403 across this many consecutive authenticated calls trips
# the auth-invalid flag. Chosen so both desktop (mixed long-poll + 30s idle)
# and Android (10s) surface the banner within ~30-90 s of real failure —
# comfortably above transient hiccups (mid-flight deploys, a single dropped
# packet), comfortably below user tolerance.
AUTH_FAILURE_THRESHOLD = 3


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

        # Auth-invalid tracking. Orthogonal to ConnectionState: an auth-broken
        # device can still reach /api/health (optional-auth) and show as
        # CONNECTED at the HTTP layer. The banner shows whenever
        # `auth_failure_kind is not None`.
        self._auth_failure_count = 0
        self._auth_failure_kind: AuthFailureKind | None = None
        self._auth_streak_kind: AuthFailureKind | None = None
        self._auth_failure_callbacks: list = []

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def effective_state(self) -> ConnectionState:
        """State for UI consumption. "Online" means both network AND
        credentials work — a latched auth-invalid flag OR an active failure
        streak (even a single 401/403 pending confirmation) counts as
        offline. We'd rather flash offline briefly and upgrade to online on
        a verified success than the other way around."""
        with self._lock:
            if self._auth_failure_kind is not None:
                return ConnectionState.DISCONNECTED
            if self._auth_failure_count > 0:
                return ConnectionState.DISCONNECTED
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

    def on_auth_failure(self, callback) -> None:
        """Callback(kind: AuthFailureKind) fires once when the auth-failure
        counter crosses AUTH_FAILURE_THRESHOLD. Does NOT fire again for
        repeated failures of the same kind — the flag stays latched until
        `clear_auth_failure()` runs."""
        self._auth_failure_callbacks.append(callback)

    @property
    def auth_failure_kind(self) -> "AuthFailureKind | None":
        with self._lock:
            return self._auth_failure_kind

    def clear_auth_failure(self) -> None:
        """Clear the latched auth-invalid flag. Called after the user accepts
        the banner and local credentials are wiped (or new ones take effect)."""
        with self._lock:
            previous = self._compute_effective_state_locked()
            self._auth_failure_count = 0
            self._auth_failure_kind = None
        self._notify_effective_state_change(previous)

    def _record_auth_response(self, status_code: int) -> None:
        """Called from request() after every authenticated response.

        Maintains the consecutive-failure counter: a 2xx resets it, a 401
        or 403 advances it, and crossing AUTH_FAILURE_THRESHOLD latches the
        kind into `_auth_failure_kind` (cleared only by the user, via
        clear_auth_failure). Notifies state observers whenever effective
        state transitions; fires auth-failure callbacks exactly once on the
        latching transition."""
        kind: "AuthFailureKind | None"
        if status_code == 401:
            kind = AuthFailureKind.CREDENTIALS_INVALID
        elif status_code == 403:
            kind = AuthFailureKind.PAIRING_MISSING
        else:
            kind = None

        # Single critical section so other threads can't interleave counter
        # changes between the success-clear and failure-increment paths.
        tripped_kind: "AuthFailureKind | None" = None
        with self._lock:
            previous_effective = self._compute_effective_state_locked()
            if kind is None:
                # Server accepted our creds — reset the streak. The latched
                # flag itself stays; only clear_auth_failure() removes that.
                self._auth_failure_count = 0
                self._auth_streak_kind = None
            elif self._auth_failure_kind is not None:
                # Already latched; no counter work, no re-fire.
                return
            else:
                if self._auth_streak_kind != kind:
                    # 401 after a run of 403s (or vice versa) — reset the
                    # streak to the new kind so we don't mix signals.
                    self._auth_streak_kind = kind
                    self._auth_failure_count = 1
                else:
                    self._auth_failure_count += 1
                if self._auth_failure_count >= AUTH_FAILURE_THRESHOLD:
                    self._auth_failure_kind = kind
                    tripped_kind = kind
        self._notify_effective_state_change(previous_effective)

        if tripped_kind is not None:
            log.warning("auth.failure.tripped kind=%s count=%d",
                        tripped_kind.value, AUTH_FAILURE_THRESHOLD)
            for cb in list(self._auth_failure_callbacks):
                try:
                    cb(tripped_kind)
                except Exception:
                    log.exception("auth failure callback error")

    def _set_state(self, new_state: ConnectionState) -> None:
        with self._lock:
            old_effective = self._compute_effective_state_locked()
            self._state = new_state
            new_effective = self._compute_effective_state_locked()
        if old_effective != new_effective:
            for cb in self._callbacks:
                try:
                    cb(new_effective)
                except Exception:
                    log.exception("State change callback error")

    def _compute_effective_state_locked(self) -> ConnectionState:
        # Must mirror the effective_state property exactly — any divergence
        # means observers and readers disagree about when to flip.
        if self._auth_failure_kind is not None:
            return ConnectionState.DISCONNECTED
        if self._auth_failure_count > 0:
            return ConnectionState.DISCONNECTED
        return self._state

    def _notify_effective_state_change(self, previous: ConnectionState) -> None:
        new_effective = self.effective_state
        if previous == new_effective:
            return
        for cb in list(self._callbacks):
            try:
                cb(new_effective)
            except Exception:
                log.exception("State change callback error")

    def update_credentials(self, device_id: str, auth_token: str) -> None:
        """Swap the credentials used for auth_headers() atomically. Called
        after a re-register so in-flight requests on other threads don't
        observe a half-updated (device_id, auth_token) pair."""
        with self._lock:
            self.device_id = device_id
            self.auth_token = auth_token

    def auth_headers(self) -> dict:
        with self._lock:
            return {
                "X-Device-ID": self.device_id,
                "Authorization": f"Bearer {self.auth_token}",
            }

    def check_connection(self) -> bool:
        """Heartbeat using an authenticated endpoint. Returning True means
        both network AND credentials work — /api/health with optional auth
        used to 200-back even on bad creds, which is how the app ended up
        claiming "online" before any request was actually accepted."""
        self._set_state(ConnectionState.RECONNECTING)
        resp = self.request("GET", "/api/transfers/pending", timeout=5)
        return resp is not None and 200 <= resp.status_code < 300

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
            attempt = self._retry_count
            delay = self._current_backoff
        level = log.warning if attempt > 3 else log.info
        level(
            "connection.backoff.retry attempt=%d delay_seconds=%.1f",
            attempt, delay,
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
        # Auth-specific copy takes precedence — distinguishes 401 (server
        # doesn't recognise us) from 403 (authed but no pairing row).
        # Mirrors the Android per-kind banner messages.
        kind = self.auth_failure_kind
        if kind == AuthFailureKind.CREDENTIALS_INVALID:
            return "Offline - server doesn't recognise this device"
        if kind == AuthFailureKind.PAIRING_MISSING:
            return "Offline - pairing lost on server"
        state = self.effective_state
        if state == ConnectionState.CONNECTED:
            return "Connected"
        if state == ConnectionState.RECONNECTING:
            return "Connecting..."
        with self._lock:
            retry = self._retry_count
            secs = max(0, int(self._next_retry_at - time.time()))
        return f"Offline - retry #{retry} in {secs}s"

    def request(self, method: str, path: str, track_state: bool = True,
                **kwargs) -> requests.Response | None:
        """
        Make an authenticated request. Returns response on success,
        None on connection failure (and updates backoff state).

        ``track_state=False`` skips the connection-state updates (no
        CONNECTED-on-success, no backoff-on-failure) while still feeding
        the auth-failure counter. Use for advisory calls on hot loops
        with aggressive timeouts — e.g. the delivery tracker's 750 ms
        ``get_sent_status`` — where a missed tick shouldn't be interpreted
        as "server is down", only "try again next cycle".
        """
        url = f"{self.server_url}{path}"
        kwargs.setdefault("headers", {}).update(self.auth_headers())
        kwargs.setdefault("timeout", 30)
        try:
            resp = requests.request(method, url, **kwargs)
            if track_state:
                # Only a verified-authed response flips the state machine
                # into CONNECTED. A 4xx means we reached the server but it
                # deliberately rejected this specific request — auth, rate
                # limit, validation, etc. None of those are evidence the
                # connection is broken. Don't spin backoff on them; leave
                # state alone and let the auth counter / application layer
                # decide what to do.
                # 5xx = server trouble, back off so we don't hammer.
                if 200 <= resp.status_code < 300:
                    self._on_success()
                elif resp.status_code >= 500:
                    self._on_failure()
                # 4xx (including 401/403): state unchanged. The auth
                # counter still tracks 401/403 via _record_auth_response
                # below, and effective_state surfaces the pending streak
                # regardless of state.
            self._record_auth_response(resp.status_code)
            return resp
        except requests.RequestException as e:
            if track_state:
                log.warning("Request failed: %s %s: %s", method, path, e)
                self._on_failure()
            return None
