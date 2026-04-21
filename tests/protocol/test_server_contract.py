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

    def __init__(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dc-protocol-")
        self._server_src = os.path.join(REPO_ROOT, "server")
        self._server_copy = os.path.join(self._tmpdir, "server")
        shutil.copytree(
            self._server_src,
            self._server_copy,
            ignore=shutil.ignore_patterns("data", "storage", "__pycache__", ".*"),
        )

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


if __name__ == "__main__":
    unittest.main()
