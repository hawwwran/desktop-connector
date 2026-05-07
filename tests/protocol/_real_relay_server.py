"""Long-lived PHP relay server for end-to-end tests.

Spins up a hermetic copy of ``server/`` on a fixed port (default 4411) so
tests can exercise the real HTTP path through ``public/index.php``. Each
``start()`` is tabula-rasa: a fresh tempdir copy of ``server/`` with no
``data/`` or ``storage/`` carry-over, so runs are deterministic.

Designed to be driven from ``setUpModule`` / ``tearDownModule`` so the PHP
process is shared by every test in a module — one PHP boot per module
instead of per test class. ``atexit`` is also registered so a crashed test
run still cleans up the listener.

Why a real-path harness exists alongside the per-class ``_ServerHarness``
in ``test_server_contract.py``: end-to-end tests need to catch
``require_once``/autoload gaps in the front controller (e.g. the missing
``Crypto/VaultCrypto.php`` require that broke production manifest publish).
Mocked relays never load ``index.php`` and miss those failures.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


DEFAULT_PORT = 4411


class RealRelayServer:
    """Manages one PHP relay process bound to a fixed port."""

    def __init__(self, *, port: int = DEFAULT_PORT, server_src: str | None = None) -> None:
        self.port = int(port)
        if server_src is None:
            here = os.path.dirname(os.path.abspath(__file__))
            server_src = os.path.abspath(os.path.join(here, "..", "..", "server"))
        self._server_src = server_src
        self._tmpdir: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self.base_url = f"http://127.0.0.1:{self.port}"

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return  # already running
        if not self._port_is_free():
            raise RuntimeError(
                f"port {self.port} is already in use — stop the leftover "
                f"process (e.g. `fuser -k {self.port}/tcp`) and re-run"
            )
        self._tmpdir = tempfile.mkdtemp(prefix="dc-real-relay-")
        server_copy = os.path.join(self._tmpdir, "server")
        shutil.copytree(
            self._server_src,
            server_copy,
            ignore=shutil.ignore_patterns("data", "storage", "__pycache__", ".*"),
        )
        cmd = ["php", "-S", f"127.0.0.1:{self.port}", "-t", "public"]
        self._proc = subprocess.Popen(
            cmd,
            cwd=server_copy,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        atexit.register(self.stop)
        try:
            self._wait_until_ready()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
        self._proc = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    # -- internals ---------------------------------------------------------

    def _port_is_free(self) -> bool:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", self.port))
                return True
            except OSError:
                return False

    def _wait_until_ready(self) -> None:
        deadline = time.time() + 8
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                stderr = ""
                if self._proc.stderr is not None:
                    try:
                        stderr = self._proc.stderr.read(4096) or ""
                    except Exception:
                        pass
                raise RuntimeError(
                    f"PHP relay exited before becoming ready (rc={self._proc.returncode}); "
                    f"stderr: {stderr.strip() or '<empty>'}"
                )
            try:
                req = urllib.request.Request(self.base_url + "/api/health")
                with urllib.request.urlopen(req, timeout=1) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, socket.timeout):
                time.sleep(0.1)
        raise RuntimeError(f"PHP relay did not become ready on port {self.port}")

    # -- request helper ---------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        json_body: dict | None = None,
        raw_body: bytes | None = None,
        token: str | None = None,
        device_id: str | None = None,
        timeout: float = 15.0,
    ):
        """Issue an HTTP request and return ``(status, headers, body)``.

        ``body`` is parsed as JSON when possible; otherwise returned as a
        decoded string. ``token``/``device_id`` shortcuts populate the
        ``Authorization`` and ``X-Device-Id`` headers.
        """
        h: dict[str, str] = dict(headers or {})
        if token is not None:
            h.setdefault("Authorization", f"Bearer {token}")
        if device_id is not None:
            h.setdefault("X-Device-Id", device_id)

        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        elif raw_body is not None:
            data = raw_body

        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers=h,
            data=data,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, dict(resp.headers), self._parse(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, dict(e.headers or {}), self._parse(body)

    @staticmethod
    def _parse(body: bytes):
        if not body:
            return ""
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body.decode("utf-8", errors="replace")


# --- module-level singleton -------------------------------------------------

_SHARED: RealRelayServer | None = None


def get_shared_server() -> RealRelayServer:
    """Return a process-wide singleton server instance.

    Lazy-construction: first caller picks the port (default 4411). Use from
    ``setUpModule()``::

        from _real_relay_server import get_shared_server

        def setUpModule():
            get_shared_server().start()

        def tearDownModule():
            get_shared_server().stop()

    Subsequent ``start()`` calls are no-ops while the process is alive,
    so individual test modules can safely call ``start()`` from their own
    ``setUpModule`` even when several real-path modules are loaded together.
    """
    global _SHARED
    if _SHARED is None:
        _SHARED = RealRelayServer()
    return _SHARED
