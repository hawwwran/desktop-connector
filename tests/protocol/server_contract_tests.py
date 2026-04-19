import atexit
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request


class _ServerHarness:
    def __init__(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dc-protocol-")
        self._repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self._server_src = os.path.join(self._repo_root, "server")
        self._server_copy = os.path.join(self._tmpdir, "server")
        shutil.copytree(self._server_src, self._server_copy)

        self._port = self._pick_port()
        self.base_url = f"http://127.0.0.1:{self._port}"
        self._proc: subprocess.Popen[str] | None = None

    @staticmethod
    def _pick_port() -> int:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def start(self) -> None:
        cmd = ["php", "-S", f"127.0.0.1:{self._port}", "-t", "public"]
        self._proc = subprocess.Popen(
            cmd,
            cwd=self._server_copy,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._wait_until_ready()
        atexit.register(self.stop)

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=3)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _wait_until_ready(self) -> None:
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                status, _headers, _body = self.request("GET", "/api/health")
                if status == 200:
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("PHP server did not become ready in time")

    def request(self, method: str, path: str, *, token: str | None = None, device_id: str | None = None, json_body: dict | None = None, raw_body: bytes | None = None):
        headers: dict[str, str] = {}
        data = None
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if device_id is not None:
            headers["X-Device-Id"] = device_id

        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            data = raw_body

        req = urllib.request.Request(self.base_url + path, method=method, headers=headers, data=data)

        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                payload = res.read().decode("utf-8")
                content_type = res.headers.get("Content-Type", "")
                body = json.loads(payload) if "application/json" in content_type else payload
                return res.status, dict(res.headers), body
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8")
            content_type = e.headers.get("Content-Type", "")
            body = json.loads(payload) if "application/json" in content_type and payload else payload
            return e.code, dict(e.headers), body


class ServerProtocolContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ServerHarness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def _register_device(self, device_type: str):
        raw_key = os.urandom(32)
        public_key = base64.b64encode(raw_key).decode("ascii")
        status, _headers, body = self.h.request(
            "POST",
            "/api/devices/register",
            json_body={"public_key": public_key, "device_type": device_type},
        )
        self.assertEqual(status, 201)
        self.assertIn("device_id", body)
        self.assertIn("auth_token", body)
        return body["device_id"], body["auth_token"], public_key

    def test_register_contract_and_reregister(self):
        device_id, auth_token, public_key = self._register_device("desktop")
        self.assertEqual(len(device_id), 32)
        self.assertEqual(len(auth_token), 64)

        status, _headers, body = self.h.request(
            "POST",
            "/api/devices/register",
            json_body={"public_key": public_key, "device_type": "desktop"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], device_id)
        self.assertEqual(body["auth_token"], auth_token)

    def test_pairing_contract(self):
        desktop_id, desktop_token, _ = self._register_device("desktop")
        phone_id, phone_token, phone_pub = self._register_device("phone")

        status, _headers, body = self.h.request(
            "POST",
            "/api/pairing/request",
            token=phone_token,
            device_id=phone_id,
            json_body={"desktop_id": desktop_id, "phone_pubkey": phone_pub},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "ok")

        status, _headers, body = self.h.request(
            "GET",
            "/api/pairing/poll",
            token=desktop_token,
            device_id=desktop_id,
        )
        self.assertEqual(status, 200)
        self.assertIn("requests", body)
        self.assertGreaterEqual(len(body["requests"]), 1)

        req = body["requests"][0]
        self.assertEqual(req["phone_id"], phone_id)
        self.assertIn("phone_pubkey", req)

        status, _headers, body = self.h.request(
            "POST",
            "/api/pairing/confirm",
            token=desktop_token,
            device_id=desktop_id,
            json_body={"phone_id": phone_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_transfer_sent_status_notify_and_fasttrack_contracts(self):
        desktop_id, desktop_token, _ = self._register_device("desktop")
        phone_id, phone_token, phone_pub = self._register_device("phone")

        self.h.request(
            "POST",
            "/api/pairing/request",
            token=phone_token,
            device_id=phone_id,
            json_body={"desktop_id": desktop_id, "phone_pubkey": phone_pub},
        )
        self.h.request("GET", "/api/pairing/poll", token=desktop_token, device_id=desktop_id)
        self.h.request(
            "POST", "/api/pairing/confirm", token=desktop_token, device_id=desktop_id, json_body={"phone_id": phone_id}
        )

        transfer_id = "tx-contract-1"
        status, _headers, body = self.h.request(
            "POST",
            "/api/transfers/init",
            token=desktop_token,
            device_id=desktop_id,
            json_body={
                "transfer_id": transfer_id,
                "recipient_id": phone_id,
                "encrypted_meta": "meta-ciphertext",
                "chunk_count": 1,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["transfer_id"], transfer_id)
        self.assertEqual(body["status"], "awaiting_chunks")

        self.h.request(
            "POST",
            f"/api/transfers/{transfer_id}/chunks/0",
            token=desktop_token,
            device_id=desktop_id,
            raw_body=b"chunk-one",
        )

        status, _headers, body = self.h.request(
            "GET", "/api/transfers/pending", token=phone_token, device_id=phone_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["transfers"]), 1)
        pending = body["transfers"][0]
        self.assertEqual(pending["transfer_id"], transfer_id)

        status, _headers, _ = self.h.request(
            "GET", f"/api/transfers/{transfer_id}/chunks/0", token=phone_token, device_id=phone_id
        )
        self.assertEqual(status, 200)

        status, _headers, body = self.h.request(
            "GET",
            "/api/transfers/sent-status",
            token=desktop_token,
            device_id=desktop_id,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["transfers"]), 1)
        sent = body["transfers"][0]
        self.assertEqual(sent["transfer_id"], transfer_id)
        self.assertIn(sent["status"], {"uploading", "pending", "delivered"})
        self.assertIn(sent["delivery_state"], {"not_started", "in_progress", "delivered"})

        status, _headers, body = self.h.request(
            "GET", "/api/transfers/notify?test=1", token=desktop_token, device_id=desktop_id
        )
        self.assertEqual(status, 200)
        self.assertIn("pending", body)
        self.assertIn("delivered", body)
        self.assertIn("download_progress", body)
        self.assertTrue(body.get("test"))

        status, _headers, body = self.h.request(
            "POST",
            "/api/fasttrack/send",
            token=desktop_token,
            device_id=desktop_id,
            json_body={
                "recipient_id": phone_id,
                "encrypted_data": "ciphertext",
            },
        )
        self.assertEqual(status, 201)
        self.assertIn("message_id", body)
        message_id = body["message_id"]

        status, _headers, body = self.h.request(
            "GET", "/api/fasttrack/pending", token=phone_token, device_id=phone_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["id"], message_id)

        status, _headers, body = self.h.request(
            "POST", f"/api/fasttrack/{message_id}/ack", token=phone_token, device_id=phone_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
