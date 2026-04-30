"""Pin the typed outcomes of the new streaming-aware desktop ApiClient
methods against the hermetic PHP server.

This file complements ``test_server_contract.py`` — that suite pins
wire behavior, this one pins the Python client's translation of wire
responses into ``ChunkUploadOutcome`` / ``ChunkDownloadOutcome`` /
``init_transfer`` / ``get_capabilities`` / ``ack_chunk`` / ``abort_transfer``.

C.1 deliverable: these tests must stay green for the rest of Phase C.
They guard the typed-outcome surface that C.3/C.4 build on.
"""

from __future__ import annotations

import base64
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

sys.path.insert(0, REPO_ROOT)

from test_server_contract import _ServerHarness  # noqa: E402

from desktop.src.api_client import (  # noqa: E402
    ApiClient,
    CAPABILITY_STREAM_V1,
    DOWNLOAD_ABORTED,
    DOWNLOAD_OK,
    DOWNLOAD_TOO_EARLY,
    UPLOAD_ABORTED,
    UPLOAD_OK,
)
from desktop.src.connection import ConnectionManager  # noqa: E402


class _ClientBoundServer:
    """Wraps ``_ServerHarness`` with two authed ``ApiClient``s so tests
    can drive the real Python code path rather than hand-built HTTP
    requests. Fresh device pair per test method.
    """

    def __init__(self, harness: _ServerHarness) -> None:
        self.h = harness

    def register(self, device_type: str) -> tuple[str, str, str]:
        # Fresh random pubkey per call so the server assigns a distinct
        # device_id. The register endpoint refuses re-registration with
        # an already-known pubkey (the auth-token-leak fix); reusing the
        # same bytes would now 409 instead of silently collapsing
        # devices.
        public_key = base64.b64encode(os.urandom(32)).decode("ascii")
        status, _headers, body = self.h.request(
            "POST", "/api/devices/register",
            json_body={
                "public_key": public_key,
                "device_type": device_type,
            },
        )
        assert status == 201, (status, body)
        return body["device_id"], body["auth_token"], public_key

    def pair(self, sender_id: str, sender_token: str,
             recipient_id: str, recipient_token: str,
             recipient_pub: str) -> None:
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

    def bump_last_seen(self, device_id: str, token: str) -> None:
        self.h.request(
            "GET", "/api/health",
            token=token, device_id=device_id,
        )

    def api_for(self, device_id: str, token: str) -> ApiClient:
        # KeyManager isn't used by any of the methods under test, so
        # we pass a stub. Kept minimal to avoid import churn.
        class _NoCrypto:
            def get_public_key_b64(self) -> str:  # pragma: no cover
                return ""
        conn = ConnectionManager(self.h.base_url, device_id, token)
        return ApiClient(conn, _NoCrypto())


class ApiClientStreamingOutcomeTests(unittest.TestCase):
    h: _ServerHarness

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ServerHarness()
        cls.h.start()
        cls.bound = _ClientBoundServer(cls.h)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def _streaming_pair(self, chunk_count: int = 3):
        sender_id, sender_token, _ = self.bound.register("desktop")
        recipient_id, recipient_token, recipient_pub = self.bound.register("phone")
        self.bound.pair(sender_id, sender_token, recipient_id, recipient_token, recipient_pub)
        self.bound.bump_last_seen(recipient_id, recipient_token)
        sender = self.bound.api_for(sender_id, sender_token)
        recipient = self.bound.api_for(recipient_id, recipient_token)
        tid = str(uuid.uuid4())
        status, negotiated = sender.init_transfer(
            tid, recipient_id, "e30=", chunk_count, mode="streaming",
        )
        self.assertEqual(status, "ok")
        self.assertEqual(negotiated, "streaming")
        return {
            "sender": sender,
            "sender_id": sender_id,
            "recipient": recipient,
            "recipient_id": recipient_id,
            "transfer_id": tid,
        }

    # --- Capability probe ------------------------------------------------

    def test_get_capabilities_returns_stream_v1(self):
        sender_id, sender_token, _ = self.bound.register("desktop")
        api = self.bound.api_for(sender_id, sender_token)
        caps = api.get_capabilities(force_refresh=True)
        self.assertIn(CAPABILITY_STREAM_V1, caps)
        self.assertTrue(api.supports_streaming())

    # --- init_transfer ---------------------------------------------------

    def test_init_classic_mode_returns_classic_negotiated(self):
        sender_id, sender_token, _ = self.bound.register("desktop")
        recipient_id, recipient_token, recipient_pub = self.bound.register("phone")
        self.bound.pair(sender_id, sender_token, recipient_id, recipient_token, recipient_pub)
        api = self.bound.api_for(sender_id, sender_token)
        tid = str(uuid.uuid4())
        status, negotiated = api.init_transfer(tid, recipient_id, "e30=", 2)
        self.assertEqual(status, "ok")
        # Client didn't request streaming → server returns classic.
        self.assertEqual(negotiated, "classic")

    def test_init_streaming_online_recipient(self):
        ctx = self._streaming_pair(chunk_count=2)
        # Nothing more to assert here — _streaming_pair already checked
        # (status, negotiated) == ("ok", "streaming").
        self.assertIsNotNone(ctx["transfer_id"])

    # --- download_chunk typed outcomes ----------------------------------

    def test_download_chunk_ok_returns_bytes(self):
        ctx = self._streaming_pair(chunk_count=2)
        # Upload chunk 0 via raw harness so download_chunk has something
        # to fetch (upload_chunk is tested separately below).
        self.h.request(
            "POST", f"/api/transfers/{ctx['transfer_id']}/chunks/0",
            token=ctx["sender"].conn.auth_token,
            device_id=ctx["sender"].conn.device_id,
            raw_body=b"encrypted-chunk-0",
        )
        outcome = ctx["recipient"].download_chunk(ctx["transfer_id"], 0)
        self.assertEqual(outcome.status, DOWNLOAD_OK)
        self.assertEqual(outcome.data, b"encrypted-chunk-0")
        self.assertEqual(outcome.http_status, 200)

    def test_download_chunk_too_early_carries_retry_hint(self):
        ctx = self._streaming_pair(chunk_count=3)
        # Chunk 0 uploaded so transfer surfaces in pending, but chunk 1
        # is still pending on the sender.
        self.h.request(
            "POST", f"/api/transfers/{ctx['transfer_id']}/chunks/0",
            token=ctx["sender"].conn.auth_token,
            device_id=ctx["sender"].conn.device_id,
            raw_body=b"c0",
        )
        outcome = ctx["recipient"].download_chunk(ctx["transfer_id"], 1)
        self.assertEqual(outcome.status, DOWNLOAD_TOO_EARLY)
        self.assertEqual(outcome.http_status, 425)
        # Either body or header must have populated the retry hint.
        self.assertIsNotNone(outcome.retry_after_ms)
        self.assertGreater(outcome.retry_after_ms, 0)

    def test_download_chunk_aborted_carries_reason(self):
        ctx = self._streaming_pair(chunk_count=2)
        # Sender aborts. Recipient's next GET must see 410.
        ok = ctx["sender"].abort_transfer(ctx["transfer_id"], "sender_abort")
        self.assertTrue(ok)
        outcome = ctx["recipient"].download_chunk(ctx["transfer_id"], 0)
        self.assertEqual(outcome.status, DOWNLOAD_ABORTED)
        self.assertEqual(outcome.http_status, 410)
        # Reason is optional on the wire, but when present must be a
        # known string. sender_abort is what we sent, so it should come
        # back.
        self.assertEqual(outcome.abort_reason, "sender_abort")

    # --- upload_chunk typed outcomes -------------------------------------

    def test_upload_chunk_ok_returns_body(self):
        ctx = self._streaming_pair(chunk_count=2)
        outcome = ctx["sender"].upload_chunk(ctx["transfer_id"], 0, b"c0")
        self.assertEqual(outcome.status, UPLOAD_OK)
        self.assertEqual(outcome.http_status, 200)
        self.assertIsInstance(outcome.body, dict)
        self.assertIn("chunks_received", outcome.body)

    def test_upload_chunk_after_recipient_abort_returns_aborted(self):
        ctx = self._streaming_pair(chunk_count=2)
        # Upload chunk 0 normally so transfer is rolling.
        first = ctx["sender"].upload_chunk(ctx["transfer_id"], 0, b"c0")
        self.assertEqual(first.status, UPLOAD_OK)
        # Recipient aborts.
        ok = ctx["recipient"].abort_transfer(ctx["transfer_id"], "recipient_abort")
        self.assertTrue(ok)
        # Next chunk upload must see 410.
        second = ctx["sender"].upload_chunk(ctx["transfer_id"], 1, b"c1")
        self.assertEqual(second.status, UPLOAD_ABORTED)
        self.assertEqual(second.http_status, 410)
        self.assertEqual(second.abort_reason, "recipient_abort")

    # --- ack_chunk -------------------------------------------------------

    def test_ack_chunk_succeeds_for_streaming_transfer(self):
        ctx = self._streaming_pair(chunk_count=2)
        ctx["sender"].upload_chunk(ctx["transfer_id"], 0, b"c0")
        # Pull the chunk down so server considers it served-ready for ACK.
        dl = ctx["recipient"].download_chunk(ctx["transfer_id"], 0)
        self.assertEqual(dl.status, DOWNLOAD_OK)
        ok = ctx["recipient"].ack_chunk(ctx["transfer_id"], 0)
        self.assertTrue(ok)

    def test_ack_chunk_rejected_for_classic_transfer(self):
        sender_id, sender_token, _ = self.bound.register("desktop")
        recipient_id, recipient_token, recipient_pub = self.bound.register("phone")
        self.bound.pair(sender_id, sender_token, recipient_id, recipient_token, recipient_pub)
        sender = self.bound.api_for(sender_id, sender_token)
        recipient = self.bound.api_for(recipient_id, recipient_token)
        tid = str(uuid.uuid4())
        status, negotiated = sender.init_transfer(tid, recipient_id, "e30=", 1)
        self.assertEqual(status, "ok")
        self.assertEqual(negotiated, "classic")
        sender.upload_chunk(tid, 0, b"c0")
        # ack_chunk on classic mode → 400 from server → False here.
        self.assertFalse(recipient.ack_chunk(tid, 0))

    # --- abort_transfer (plus back-compat cancel_transfer) --------------

    def test_abort_transfer_by_sender(self):
        ctx = self._streaming_pair(chunk_count=2)
        ok = ctx["sender"].abort_transfer(ctx["transfer_id"], "sender_abort")
        self.assertTrue(ok)
        # Server keeps the transfer row after abort (only sets
        # aborted=1 and wipes blobs), so a second DELETE from the same
        # sender is idempotent — still returns 200 with status=aborted.
        # Client reports True; callers must not assume single-success.
        second = ctx["sender"].abort_transfer(ctx["transfer_id"], "sender_abort")
        self.assertTrue(second)

    def test_abort_transfer_by_recipient(self):
        ctx = self._streaming_pair(chunk_count=2)
        ok = ctx["recipient"].abort_transfer(ctx["transfer_id"], "recipient_abort")
        self.assertTrue(ok)

    def test_abort_rejects_cross_role_reason(self):
        ctx = self._streaming_pair(chunk_count=2)
        # Recipient tries to claim sender_abort → 400 → False.
        bad = ctx["recipient"].abort_transfer(ctx["transfer_id"], "sender_abort")
        self.assertFalse(bad)

    def test_cancel_transfer_back_compat_still_works(self):
        """Legacy cancel_transfer wraps abort_transfer(sender_abort)."""
        ctx = self._streaming_pair(chunk_count=2)
        ok = ctx["sender"].cancel_transfer(ctx["transfer_id"])
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
