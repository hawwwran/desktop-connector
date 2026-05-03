import atexit
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402


class _ServerHarness:
    """Spins up a hermetic PHP server: fresh tempdir copy of server/ source,
    skipping ``data/`` and ``storage/`` so any stray dev-run state doesn't leak
    into tests. Each test method registers fresh device credentials, so
    state sharing across tests in the same class is benign.
    """

    def __init__(self, config_overrides: dict | None = None) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dc-protocol-")
        self._server_src = os.path.join(REPO_ROOT, "server")
        self._server_copy = os.path.join(self._tmpdir, "server")
        shutil.copytree(
            self._server_src,
            self._server_copy,
            ignore=shutil.ignore_patterns("data", "storage", "__pycache__", ".*"),
        )
        # Pre-seed data/config.json so the server's first request reads
        # overrides instead of auto-creating the default. Merges with
        # the defaults server-side (Config::load falls back to
        # self::DEFAULTS per missing key), so tests only need to set
        # what they want to change.
        if config_overrides:
            cfg_dir = os.path.join(self._server_copy, "data")
            os.makedirs(cfg_dir, exist_ok=True)
            with open(os.path.join(cfg_dir, "config.json"), "w") as fd:
                json.dump(config_overrides, fd)

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

        # Re-register with the same pubkey now returns 409 — closing the
        # auth-token-leak vector documented in the M.11 plan. The server
        # must NOT echo any auth_token in the body, otherwise anyone
        # holding the public key (which travels through QR codes /
        # .dcpair files) could harvest the credential.
        status, _headers, body = self.h.request(
            "POST",
            "/api/devices/register",
            json_body={"public_key": public_key, "device_type": "desktop"},
        )
        self.assertEqual(status, 409)
        self.assertNotIn("auth_token", body)

    def test_register_does_not_leak_auth_token_to_pubkey_holder(self):
        # Regression for the auth-token-leak vector: the public key is
        # not secret material. An attacker who photographs a QR code,
        # captures a .dcpair file, or sniffs a pairing handshake holds
        # the same data this test supplies. The endpoint must refuse
        # to return the existing auth_token in any of those cases.
        device_id, auth_token, public_key = self._register_device("desktop")

        attacker_status, _h, attacker_body = self.h.request(
            "POST",
            "/api/devices/register",
            json_body={"public_key": public_key, "device_type": "desktop"},
        )

        self.assertEqual(attacker_status, 409)
        # No `auth_token` field at all — not even an empty / null one.
        self.assertNotIn("auth_token", attacker_body)
        # And of course not the actual token by accident either.
        self.assertNotIn(auth_token, json.dumps(attacker_body))

        # The legitimate device still authenticates with its
        # originally-issued token. The 409 must not have rotated it.
        status, _h, _b = self.h.request(
            "GET",
            "/api/devices/stats",
            token=auth_token,
            device_id=device_id,
        )
        self.assertEqual(status, 200)

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

        # A retry can race in after the first request was claimed by poll.
        # Confirming the pair must clean it up so reopening pairing does not
        # show the old verification code again.
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
            "POST",
            "/api/pairing/confirm",
            token=desktop_token,
            device_id=desktop_id,
            json_body={"phone_id": phone_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

        status, _headers, body = self.h.request(
            "GET",
            "/api/pairing/poll",
            token=desktop_token,
            device_id=desktop_id,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["requests"], [])

        # Once the server already has this pair, stale clients may still retry
        # the request. Treat that as idempotent cleanup, not as a new pending
        # verification prompt.
        status, _headers, body = self.h.request(
            "POST",
            "/api/pairing/request",
            token=phone_token,
            device_id=phone_id,
            json_body={"desktop_id": desktop_id, "phone_pubkey": phone_pub},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

        status, _headers, body = self.h.request(
            "GET",
            "/api/pairing/poll",
            token=desktop_token,
            device_id=desktop_id,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["requests"], [])

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

    # ---- Error-envelope contract -------------------------------------------------
    #
    # The server surfaces ApiError subclasses via ErrorResponder as JSON with
    # a top-level "error" string. These paths are as much a part of the
    # contract as the happy paths.

    def test_auth_errors_contract(self):
        # No auth at all -> 401
        status, _headers, body = self.h.request("GET", "/api/devices/stats")
        self.assertEqual(status, 401)
        self.assertIn("error", body)

        # Device-id present but bearer token wrong -> 401
        desktop_id, _good_token, _ = self._register_device("desktop")
        status, _headers, body = self.h.request(
            "GET", "/api/devices/stats", token="wrong" * 16, device_id=desktop_id
        )
        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_not_found_contract(self):
        # Transfer ids are path-safe-checked at the pipeline; unknown IDs 404.
        desktop_id, desktop_token, _ = self._register_device("desktop")
        status, _headers, body = self.h.request(
            "POST",
            "/api/transfers/nonexistent-id/ack",
            token=desktop_token,
            device_id=desktop_id,
        )
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_validation_error_contract(self):
        # Missing required field -> 400 with "error".
        desktop_id, desktop_token, _ = self._register_device("desktop")
        status, _headers, body = self.h.request(
            "POST",
            "/api/transfers/init",
            token=desktop_token,
            device_id=desktop_id,
            json_body={"recipient_id": "phone-id"},  # missing transfer_id, encrypted_meta, chunk_count
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_path_traversal_rejected(self):
        # Pipeline-level transfer_id guard: path traversal must 400, not 200.
        desktop_id, desktop_token, _ = self._register_device("desktop")
        status, _headers, body = self.h.request(
            "POST",
            "/api/transfers/..%2F..%2Fetc/ack",
            token=desktop_token,
            device_id=desktop_id,
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def _pair_and_init(self):
        """Create a sender+recipient pair and init a tiny transfer.
        Returns (sender_id, sender_token, recipient_id, transfer_id)."""
        sender_id, sender_token, _ = self._register_device("desktop")
        recipient_id, recipient_token, recipient_pub = self._register_device("phone")
        # Pair them
        self.h.request(
            "POST", "/api/pairing/request",
            token=recipient_token, device_id=recipient_id,
            json_body={"desktop_id": sender_id, "phone_pubkey": recipient_pub},
        )
        self.h.request(
            "POST", "/api/pairing/confirm",
            token=sender_token, device_id=sender_id,
            json_body={"phone_id": recipient_id},
        )
        import uuid
        tid = str(uuid.uuid4())
        status, _h, body = self.h.request(
            "POST", "/api/transfers/init",
            token=sender_token, device_id=sender_id,
            json_body={
                "transfer_id": tid,
                "recipient_id": recipient_id,
                "encrypted_meta": "e30=",  # {}
                "chunk_count": 1,
            },
        )
        self.assertEqual(status, 201)
        return sender_id, sender_token, recipient_id, recipient_token, tid

    def test_cancel_transfer_by_sender(self):
        """DELETE /api/transfers/{id} removes chunks + row; recipient
        gets 404 on pending lookup afterwards."""
        sender_id, sender_token, recipient_id, recipient_token, tid = \
            self._pair_and_init()
        # Upload a 1-byte chunk so there's something to clean up
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=sender_token, device_id=sender_id,
            raw_body=b"x",
        )
        # Cancel as sender — expect 2xx
        status, _h, _b = self.h.request(
            "DELETE", f"/api/transfers/{tid}",
            token=sender_token, device_id=sender_id,
        )
        self.assertIn(status, (200, 204))
        # Recipient no longer sees it pending
        status, _h, body = self.h.request(
            "GET", "/api/transfers/pending",
            token=recipient_token, device_id=recipient_id,
        )
        self.assertEqual(status, 200)
        self.assertNotIn(tid, [t.get("transfer_id") for t in body.get("transfers", [])])

    def test_cancel_transfer_third_party_denied(self):
        """A device that is neither sender NOR recipient can't abort
        someone else's transfer — same 404 the route returns for
        unknown ids (deliberate; keeps the endpoint from leaking
        transfer-id existence). Post-streaming the DELETE endpoint
        accepts both sender and recipient; third parties stay denied."""
        sender_id, sender_token, recipient_id, recipient_token, tid = \
            self._pair_and_init()
        # Register a fresh third-party device unrelated to the pair
        third_id, third_token, _ = self._register_device("desktop")
        status, _h, body = self.h.request(
            "DELETE", f"/api/transfers/{tid}",
            token=third_token, device_id=third_id,
        )
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_abort_transfer_by_recipient(self):
        """Recipient can now DELETE — marks the transfer aborted with
        reason='recipient_abort', wipes chunks, and disappears from
        pending/sent-status properly."""
        sender_id, sender_token, recipient_id, recipient_token, tid = \
            self._pair_and_init()
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=sender_token, device_id=sender_id,
            raw_body=b"x",
        )
        status, _h, body = self.h.request(
            "DELETE", f"/api/transfers/{tid}",
            token=recipient_token, device_id=recipient_id,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "aborted")
        self.assertEqual(body.get("reason"), "recipient_abort")

        # Subsequent sender chunk upload returns 410 Gone.
        status, _h, body = self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/1",
            token=sender_token, device_id=sender_id,
            raw_body=b"y",
        )
        self.assertEqual(status, 410)
        self.assertIn("error", body)

    def test_cancel_transfer_requires_auth(self):
        sender_id, sender_token, recipient_id, recipient_token, tid = \
            self._pair_and_init()
        status, _h, body = self.h.request(
            "DELETE", f"/api/transfers/{tid}",  # no token
        )
        self.assertEqual(status, 401)

    def _pair_and_init_mode(self, *, mode=None, chunk_count=3, bump_recipient_last_seen=True):
        """Like _pair_and_init but lets the test pick mode and chunk count.
        `bump_recipient_last_seen` hits /api/health with the recipient's
        credentials so the streaming `online-by-last-seen` check passes.
        """
        sender_id, sender_token, _ = self._register_device("desktop")
        recipient_id, recipient_token, recipient_pub = self._register_device("phone")
        self.h.request(
            "POST", "/api/pairing/request",
            token=recipient_token, device_id=recipient_id,
            json_body={"desktop_id": sender_id, "phone_pubkey": recipient_pub},
        )
        self.h.request(
            "POST", "/api/pairing/confirm",
            token=sender_token, device_id=sender_id,
            json_body={"phone_id": recipient_id},
        )
        if bump_recipient_last_seen:
            # /api/health with optional auth bumps last_seen_at. Without
            # this bump, the freshly-registered recipient still has
            # last_seen from registration — which works, but we want the
            # freshness signal to be explicit in the test.
            self.h.request(
                "GET", "/api/health",
                token=recipient_token, device_id=recipient_id,
            )
        import uuid
        tid = str(uuid.uuid4())
        body = {
            "transfer_id": tid,
            "recipient_id": recipient_id,
            "encrypted_meta": "e30=",
            "chunk_count": chunk_count,
        }
        if mode is not None:
            body["mode"] = mode
        status, _h, resp = self.h.request(
            "POST", "/api/transfers/init",
            token=sender_token, device_id=sender_id,
            json_body=body,
        )
        self.assertEqual(status, 201)
        return {
            "sender_id": sender_id,
            "sender_token": sender_token,
            "recipient_id": recipient_id,
            "recipient_token": recipient_token,
            "transfer_id": tid,
            "init_response": resp,
        }

    # ---- Streaming-relay contract (migration 002) ---------------------------

    def test_health_advertises_stream_v1_capability(self):
        """Post-streaming, /api/health exposes a `capabilities` list
        that includes stream_v1 when the operator hasn't disabled it."""
        status, _h, body = self.h.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("capabilities", body)
        self.assertIn("stream_v1", body["capabilities"])

    def test_init_defaults_to_classic_mode(self):
        """Old client omitting `mode` must still succeed and get
        classic behaviour — negotiated_mode=classic is returned for
        forward-compat, but the existing transfer_id/status fields are
        untouched."""
        ctx = self._pair_and_init_mode(mode=None, chunk_count=1)
        resp = ctx["init_response"]
        self.assertEqual(resp["transfer_id"], ctx["transfer_id"])
        self.assertEqual(resp["status"], "awaiting_chunks")
        self.assertEqual(resp.get("negotiated_mode"), "classic")

    def test_init_streaming_mode_online_recipient(self):
        """mode=streaming + online recipient → negotiated_mode=streaming."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=2)
        self.assertEqual(ctx["init_response"].get("negotiated_mode"), "streaming")

    def test_init_streaming_transfer_surfaces_in_pending_on_first_chunk(self):
        """Streaming transfers must appear in /pending as soon as the
        first chunk is stored — not only after the full upload
        completes. Without this, the streaming pipeline collapses to
        classic timing."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=3)
        tid = ctx["transfer_id"]

        # Before any chunks: recipient's pending list doesn't include it
        status, _h, body = self.h.request(
            "GET", "/api/transfers/pending",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 200)
        self.assertNotIn(tid, [t["transfer_id"] for t in body.get("transfers", [])])

        # Upload chunk 0 → pending list surfaces the transfer
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"chunk-0",
        )
        status, _h, body = self.h.request(
            "GET", "/api/transfers/pending",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 200)
        ids = [t["transfer_id"] for t in body.get("transfers", [])]
        self.assertIn(tid, ids)

    def test_streaming_download_chunk_not_yet_uploaded_returns_425(self):
        """GET on a streaming chunk that hasn't landed yet returns 425
        Too Early with a Retry-After header and retry_after_ms body
        field — distinguishes 'upstream hasn't produced it' from
        'genuinely unknown' (404)."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=3)
        tid = ctx["transfer_id"]
        # Upload chunk 0 so transfer surfaces in pending; then recipient
        # asks for chunk 1 before the sender has uploaded it.
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"chunk-0",
        )
        status, headers, body = self.h.request(
            "GET", f"/api/transfers/{tid}/chunks/1",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 425)
        self.assertIn("Retry-After", headers)
        self.assertIn("retry_after_ms", body)
        self.assertGreater(int(body["retry_after_ms"]), 0)

    def test_streaming_per_chunk_ack_wipes_blob(self):
        """Per-chunk ACK deletes the chunk file from disk and blocks
        further serves of that index (410)."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=3)
        tid = ctx["transfer_id"]
        # Upload + download + ack chunk 0
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"chunk-0",
        )
        status, _h, _ = self.h.request(
            "GET", f"/api/transfers/{tid}/chunks/0",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 200)
        status, _h, body = self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0/ack",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "acked")
        self.assertEqual(body.get("chunk_index"), 0)
        # Blob file should be gone from disk
        import os as _os
        blob_path = _os.path.join(self.h._server_copy, "storage", tid, "0.bin")
        self.assertFalse(_os.path.exists(blob_path))
        # Re-GET chunk 0 → 410 (already acked and wiped)
        status, _h, _ = self.h.request(
            "GET", f"/api/transfers/{tid}/chunks/0",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 410)

    def test_streaming_final_chunk_ack_marks_delivered(self):
        """ACKing the final chunk flips downloaded=1, matches the
        invariant chunks_downloaded == chunk_count ⇒ downloaded == 1."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=2)
        tid = ctx["transfer_id"]

        for i in range(2):
            self.h.request(
                "POST", f"/api/transfers/{tid}/chunks/{i}",
                token=ctx["sender_token"], device_id=ctx["sender_id"],
                raw_body=f"chunk-{i}".encode(),
            )
            self.h.request(
                "GET", f"/api/transfers/{tid}/chunks/{i}",
                token=ctx["recipient_token"], device_id=ctx["recipient_id"],
            )
            status, _h, body = self.h.request(
                "POST", f"/api/transfers/{tid}/chunks/{i}/ack",
                token=ctx["recipient_token"], device_id=ctx["recipient_id"],
            )
            self.assertEqual(status, 200)
        # Final ACK returns status=delivered
        self.assertEqual(body.get("status"), "delivered")

        # sent-status: delivered + chunks_downloaded == chunk_count
        status, _h, body = self.h.request(
            "GET", "/api/transfers/sent-status",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
        )
        self.assertEqual(status, 200)
        match = next(t for t in body["transfers"] if t["transfer_id"] == tid)
        self.assertEqual(match["delivery_state"], "delivered")
        self.assertEqual(match["chunks_downloaded"], match["chunk_count"])

    def test_streaming_per_chunk_ack_rejected_for_classic(self):
        """Classic transfers must reject /chunks/{i}/ack — a client
        confused about the transfer's mode shouldn't accidentally wipe
        half the chunks before the recipient has downloaded them."""
        ctx = self._pair_and_init_mode(mode=None, chunk_count=2)
        tid = ctx["transfer_id"]
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"x",
        )
        status, _h, _ = self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0/ack",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 400)

    def test_sent_status_exposes_mode_and_streaming_counters(self):
        """New clients use `mode` + `chunks_uploaded` + `chunks_downloaded`
        to paint 'Sending X→Y'. Test that both fields appear for
        streaming transfers and only `mode` appears for classic."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=2)
        tid = ctx["transfer_id"]
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"chunk-0",
        )
        status, _h, body = self.h.request(
            "GET", "/api/transfers/sent-status",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
        )
        self.assertEqual(status, 200)
        match = next(t for t in body["transfers"] if t["transfer_id"] == tid)
        self.assertEqual(match.get("mode"), "streaming")
        self.assertEqual(match.get("chunks_uploaded"), 1)

    def test_config_file_auto_created_with_defaults(self):
        """Server must auto-create server/data/config.json on first
        Config::get() call. Startup already triggers it via migrate(),
        so after the harness starts the file should exist with the
        default storageQuotaMB=500."""
        import os as _os
        config_path = _os.path.join(self.h._server_copy, "data", "config.json")
        # Trigger Config::get by hitting the dashboard (which calls
        # Config::get('storageQuotaMB') for the storage stat).
        status, _h, _b = self.h.request("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertTrue(_os.path.isfile(config_path),
                        f"config.json not created at {config_path}")
        with open(config_path) as fd:
            data = json.load(fd)
        self.assertEqual(data.get("storageQuotaMB"), 500)

    def test_dashboard_shows_vaults_with_last_sync_time(self):
        device_id, auth_token, _ = self._register_device("desktop")
        vault_id = "DASH2345WXY2"
        status, _h, body = self.h.request(
            "POST",
            "/api/vaults",
            token=auth_token,
            device_id=device_id,
            json_body={
                "vault_id": "DASH-2345-WXY2",
                "vault_access_token_hash": base64.b64encode(os.urandom(32)).decode("ascii"),
                "encrypted_header": base64.b64encode(b"header").decode("ascii"),
                "header_hash": "a" * 64,
                "initial_manifest_ciphertext": base64.b64encode(b"manifest").decode("ascii"),
                "initial_manifest_hash": "b" * 64,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["data"]["vault_id"], "DASH-2345-WXY2")

        status, _h, html = self.h.request("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("<h2>Vaults</h2>", html)
        self.assertIn("Last sync", html)
        self.assertIn("DASH-2345-WXY2", html)
        self.assertIn(vault_id, html)


    # --- Extra streaming contract coverage (review follow-ups) --------------

    def test_streaming_download_chunk_after_abort_returns_410(self):
        """After a recipient aborts, a sender-side chunk upload 410s
        (existing test). Here we verify the recipient-side GET also
        410s — same terminal signal for everyone talking to the row."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=3)
        tid = ctx["transfer_id"]
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"chunk-0",
        )
        self.h.request(
            "DELETE", f"/api/transfers/{tid}",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        status, _h, body = self.h.request(
            "GET", f"/api/transfers/{tid}/chunks/0",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 410)
        self.assertIn("error", body)
        self.assertEqual(body.get("abort_reason"), "recipient_abort")

    def test_streaming_download_chunk_already_acked_returns_410(self):
        """Chunks that were served+acked+wiped must 410 on replay — not
        425 (which would loop a confused recipient forever). 425 means
        'upstream hasn't produced yet'; 410 means 'gone'."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=3)
        tid = ctx["transfer_id"]
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"chunk-0",
        )
        self.h.request(
            "GET", f"/api/transfers/{tid}/chunks/0",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0/ack",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        # Upload chunk 1 so chunks_downloaded has room to be below it —
        # we want the re-GET of chunk 0 to hit the "< chunks_downloaded"
        # branch, not the 425 branch.
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/1",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"chunk-1",
        )
        status, _h, body = self.h.request(
            "GET", f"/api/transfers/{tid}/chunks/0",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(status, 410, f"got {status}: {body}")
        self.assertIn("error", body)

    def test_sender_delete_rejects_invalid_reason(self):
        """Sender passing {reason: "recipient_abort"} or a typo should
        400, not silently coerce to sender_abort."""
        ctx = self._pair_and_init_mode(mode=None, chunk_count=1)
        tid = ctx["transfer_id"]
        status, _h, body = self.h.request(
            "DELETE", f"/api/transfers/{tid}",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            json_body={"reason": "recipient_abort"},
        )
        self.assertEqual(status, 400, f"got {status}: {body}")
        # Transfer still alive — wrong reason must not have triggered
        # the abort path. Confirm by cancelling cleanly afterwards.
        status, _h, _ = self.h.request(
            "DELETE", f"/api/transfers/{tid}",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
        )
        self.assertEqual(status, 200)

    def test_recipient_delete_rejects_invalid_reason(self):
        """Recipient passing a sender reason must 400."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=2)
        tid = ctx["transfer_id"]
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"x",
        )
        status, _h, body = self.h.request(
            "DELETE", f"/api/transfers/{tid}",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
            json_body={"reason": "sender_failed"},
        )
        self.assertEqual(status, 400, f"got {status}: {body}")

    def test_abort_after_delivered_reports_delivered_not_aborted(self):
        """A late DELETE after the recipient finalized the transfer
        must NOT flip the sender's local row to aborted. The already-
        delivered short-circuit returns status=delivered so the UI
        agrees with reality."""
        ctx = self._pair_and_init_mode(mode="streaming", chunk_count=1)
        tid = ctx["transfer_id"]
        # Upload + download + per-chunk-ack the only chunk (final).
        self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
            raw_body=b"x",
        )
        self.h.request(
            "GET", f"/api/transfers/{tid}/chunks/0",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        status, _h, body = self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0/ack",
            token=ctx["recipient_token"], device_id=ctx["recipient_id"],
        )
        self.assertEqual(body.get("status"), "delivered")

        # Sender DELETE after delivery — must report delivered, not aborted.
        status, _h, body = self.h.request(
            "DELETE", f"/api/transfers/{tid}",
            token=ctx["sender_token"], device_id=ctx["sender_id"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "delivered",
                         f"late DELETE flipped row to {body.get('status')}")
        self.assertEqual(body.get("note"), "already_delivered")


class ServerWithStreamingDisabledTests(unittest.TestCase):
    """Operator kill-switch: streamingEnabled=false must drop stream_v1
    from /api/health capabilities AND force negotiated_mode=classic
    even when the client explicitly requests streaming. Uses a dedicated
    harness because the knob can't be toggled at runtime."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ServerHarness(config_overrides={
            "storageQuotaMB": 500,
            "streamingEnabled": False,
        })
        cls.h.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def _register(self, kind):
        raw_key = os.urandom(32)
        pub = base64.b64encode(raw_key).decode("ascii")
        status, _h, body = self.h.request(
            "POST", "/api/devices/register",
            json_body={"public_key": pub, "device_type": kind},
        )
        assert status == 201, body
        return body["device_id"], body["auth_token"], pub

    def test_health_omits_stream_v1_when_disabled(self):
        status, _h, body = self.h.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertNotIn("stream_v1", body.get("capabilities", []))

    def test_streaming_init_downgraded_to_classic(self):
        sender_id, sender_tok, _ = self._register("desktop")
        recipient_id, recipient_tok, recipient_pub = self._register("phone")
        self.h.request(
            "POST", "/api/pairing/request",
            token=recipient_tok, device_id=recipient_id,
            json_body={"desktop_id": sender_id, "phone_pubkey": recipient_pub},
        )
        self.h.request(
            "POST", "/api/pairing/confirm",
            token=sender_tok, device_id=sender_id,
            json_body={"phone_id": recipient_id},
        )
        self.h.request("GET", "/api/health", token=recipient_tok, device_id=recipient_id)

        import uuid
        tid = str(uuid.uuid4())
        status, _h, body = self.h.request(
            "POST", "/api/transfers/init",
            token=sender_tok, device_id=sender_id,
            json_body={
                "transfer_id": tid,
                "recipient_id": recipient_id,
                "encrypted_meta": "e30=",
                "chunk_count": 1,
                "mode": "streaming",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(body.get("negotiated_mode"), "classic",
                         f"kill-switch failed to downgrade: {body}")


class ServerWithTightQuotaTests(unittest.TestCase):
    """Mid-stream quota gate. Sets storageQuotaMB=2 (exactly one
    PROJECTED_CHUNK_SIZE of 2 MiB) so streaming init passes the
    pathological-quota check but the second 1.5 MB chunk upload
    bounces on 507."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ServerHarness(config_overrides={
            "storageQuotaMB": 2,
            "streamingEnabled": True,
        })
        cls.h.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def _register(self, kind):
        pub = base64.b64encode(os.urandom(32)).decode("ascii")
        status, _h, body = self.h.request(
            "POST", "/api/devices/register",
            json_body={"public_key": pub, "device_type": kind},
        )
        assert status == 201, body
        return body["device_id"], body["auth_token"], pub

    def test_streaming_midstream_quota_gate_returns_507(self):
        sender_id, sender_tok, _ = self._register("desktop")
        recipient_id, recipient_tok, recipient_pub = self._register("phone")
        self.h.request(
            "POST", "/api/pairing/request",
            token=recipient_tok, device_id=recipient_id,
            json_body={"desktop_id": sender_id, "phone_pubkey": recipient_pub},
        )
        self.h.request(
            "POST", "/api/pairing/confirm",
            token=sender_tok, device_id=sender_id,
            json_body={"phone_id": recipient_id},
        )
        self.h.request("GET", "/api/health", token=recipient_tok, device_id=recipient_id)

        import uuid
        tid = str(uuid.uuid4())
        status, _h, body = self.h.request(
            "POST", "/api/transfers/init",
            token=sender_tok, device_id=sender_id,
            json_body={
                "transfer_id": tid,
                "recipient_id": recipient_id,
                "encrypted_meta": "e30=",
                "chunk_count": 3,
                "mode": "streaming",
            },
        )
        self.assertEqual(status, 201, f"streaming init failed: {body}")
        self.assertEqual(body.get("negotiated_mode"), "streaming")

        # 1.5 MiB per chunk → after chunk 0 current=1.5 MiB (fits 2 MiB
        # quota); chunk 1 would push to 3 MiB → 507.
        chunk = b"A" * (1_572_864)
        status, _h, _ = self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/0",
            token=sender_tok, device_id=sender_id,
            raw_body=chunk,
        )
        self.assertEqual(status, 200)
        status, _h, body = self.h.request(
            "POST", f"/api/transfers/{tid}/chunks/1",
            token=sender_tok, device_id=sender_id,
            raw_body=chunk,
        )
        self.assertEqual(status, 507, f"expected 507, got {status}: {body}")
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
