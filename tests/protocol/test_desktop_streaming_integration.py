"""End-to-end streaming integration tests for Phase C.

Drives the REAL desktop sender (``ApiClient.send_file(streaming=True)``)
and the REAL desktop recipient (``Poller._receive_streaming_transfer``
where the test exercises the receive loop; a simpler in-callback drain
where it exercises the sender state machine) against a hermetic PHP
server. No mocks below the HTTP layer — if this passes, a live user on
the deployed server would see the same behaviour.

Coverage:
  * Happy path — multi-chunk streaming round-trip, byte match, peak
                  on-disk stays within a chunk (blobs wiped as soon as
                  acked).
  * Sender abort mid-stream — recipient's Poller ends Aborted, .part
                  wiped.
  * Recipient abort mid-stream — sender row ends Aborted.
  * Quota gate — tight 3 MB server quota forces mid-stream 507 on
                  chunk 2; recipient drain inside the callback frees
                  the slot and the sender recovers.

Classic ``test_loop.sh`` (bash) still covers the classic path end-to-
end; these tests cover streaming. Both matter.

**Concurrency note.** The PHP built-in server (``php -S``) is single-
threaded; concurrent clients hammering it under load can hit spurious
500s. These tests therefore drive all traffic from the main thread
(sender ``send_file`` blocks; the ``on_stream_progress`` callback
interleaves recipient drain operations on the same thread). This still
exercises the full streaming lifecycle on the server — the test just
avoids the known ``php -S`` thrash that a real multi-device setup
wouldn't suffer from.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

sys.path.insert(0, REPO_ROOT)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from desktop.src import api_client as api_client_mod  # noqa: E402
from desktop.src.api_client import ApiClient  # noqa: E402
from desktop.src.connection import ConnectionManager  # noqa: E402
from desktop.src.crypto import KeyManager, CHUNK_SIZE  # noqa: E402
from desktop.src.history import TransferHistory, TransferStatus  # noqa: E402
from desktop.src.poller import Poller  # noqa: E402

from test_server_contract import _ServerHarness  # noqa: E402


# Short backoff ramp so a 507 retry in the quota-gate test doesn't cost
# seconds of wall clock per iteration. In production the 2-s floor keeps
# things sane under real traffic; in a unit test we're verifying the
# state machine, not the clock.
_FAST_QUOTA_RAMP = (0.05, 0.1, 0.2, 0.4)


class _StreamingFixture:
    """Reusable scaffolding: device pair, shared symmetric key, and
    builders for sender/receiver clients + a real Poller wired to the
    recipient credentials."""

    def __init__(self, server: _ServerHarness) -> None:
        self.h = server
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-c6-"))
        self.sender_id, self.sender_token = self._register("desktop")
        self.recipient_id, self.recipient_token = self._register("phone")
        self._pair(self.sender_id, self.sender_token,
                   self.recipient_id, self.recipient_token)
        # Bump recipient last_seen so streaming negotiation accepts.
        self.h.request("GET", "/api/health",
                       token=self.recipient_token, device_id=self.recipient_id)
        self.key = os.urandom(32)
        self.sender_keys = self.tmp / "sender_keys"
        self.sender_keys.mkdir()
        self.recipient_keys = self.tmp / "recipient_keys"
        self.recipient_keys.mkdir()

    def _register(self, kind: str) -> tuple[str, str]:
        pub = base64.b64encode(os.urandom(32)).decode()
        status, _h, body = self.h.request(
            "POST", "/api/devices/register",
            json_body={"public_key": pub, "device_type": kind},
        )
        assert status in (200, 201), (status, body)
        return body["device_id"], body["auth_token"]

    def _pair(self, sid: str, stok: str, rid: str, rtok: str) -> None:
        self.h.request(
            "POST", "/api/pairing/request",
            token=rtok, device_id=rid,
            json_body={"desktop_id": sid, "phone_pubkey": "ignored"},
        )
        self.h.request(
            "POST", "/api/pairing/confirm",
            token=stok, device_id=sid,
            json_body={"phone_id": rid},
        )

    def sender_api(self) -> ApiClient:
        conn = ConnectionManager(self.h.base_url, self.sender_id, self.sender_token)
        return ApiClient(conn, KeyManager(self.sender_keys))

    def recipient_api(self) -> ApiClient:
        conn = ConnectionManager(self.h.base_url, self.recipient_id,
                                 self.recipient_token)
        return ApiClient(conn, KeyManager(self.recipient_keys))

    def build_recipient_poller(self, history_dir: Path,
                                save_dir: Path) -> Poller:
        """A real Poller wired to the recipient's credentials. Config
        is mocked (we don't reload paired_devices from disk, and
        save_directory is a plain Path here instead of a @property)."""
        config = MagicMock()
        config.config_dir = history_dir
        config.save_directory = save_dir
        config.paired_devices = {
            self.sender_id: {
                "symmetric_key_b64": base64.b64encode(self.key).decode(),
            }
        }
        crypto = MagicMock()
        return Poller(
            config=config,
            connection=ConnectionManager(self.h.base_url, self.recipient_id,
                                          self.recipient_token),
            api=self.recipient_api(),
            crypto=crypto,
            history=TransferHistory(history_dir),
            platform=MagicMock(),
        )


def _dir_size(path: Path) -> int:
    """Sum of bytes inside a directory tree. Returns 0 if missing."""
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class StreamingHappyPathTests(unittest.TestCase):
    """End-to-end streaming transfer with in-callback recipient drain
    so the test matches the streaming invariant (blobs live briefly
    between upload and ack) without threading ``php -S``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ServerHarness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def test_streaming_roundtrip_peak_bounded(self):
        fx = _StreamingFixture(self.h)

        # 3 chunks (~6 MB). Enough to exercise the loop; not so big
        # that CI takes minutes.
        chunk_count = 3
        src = fx.tmp / "payload.bin"
        src.write_bytes(b"".join(
            bytes([(i * 31 + 7) & 0xFF])
            for i in range(chunk_count * CHUNK_SIZE - 17)  # tail-short
        ))
        src_bytes = src.read_bytes()

        sender = fx.sender_api()
        recipient_api = fx.recipient_api()

        tid_holder: list[str | None] = [None]
        peak = [0]
        ciphertext_by_index: dict[int, bytes] = {}

        def sample(tid: str) -> None:
            storage = Path(self.h._server_copy) / "storage" / tid
            size = _dir_size(storage)
            if size > peak[0]:
                peak[0] = size

        def on_stream_progress(tid, uploaded, total, state):
            tid_holder[0] = tid
            if state != "sending" or uploaded == 0:
                return
            # At this point chunk (uploaded-1) is freshly stored on the
            # server. Sample BEFORE acking so we catch the blob on
            # disk; drain to prove the streaming invariant.
            idx = uploaded - 1
            sample(tid)
            outcome = recipient_api.download_chunk(tid, idx)
            self.assertEqual(outcome.status, "ok",
                             f"chunk {idx} download failed: {outcome.status}")
            ciphertext_by_index[idx] = outcome.data
            self.assertTrue(recipient_api.ack_chunk(tid, idx),
                            f"ack_chunk({idx}) returned False")

        sent_tid = sender.send_file(
            src, fx.recipient_id, fx.key,
            on_stream_progress=on_stream_progress,
            streaming=True,
        )
        self.assertIsNotNone(sent_tid, "send_file returned None")
        self.assertEqual(tid_holder[0], sent_tid)

        # All chunks fetched + acked in order.
        self.assertEqual(sorted(ciphertext_by_index.keys()),
                         list(range(chunk_count)))

        # Peak stayed within one chunk: the recipient-side drain
        # happened inside each callback, before the next sender upload.
        self.assertLessEqual(
            peak[0], CHUNK_SIZE + 1024,
            f"peak={peak[0]:,}B exceeds one chunk; streaming may have "
            f"regressed to store-then-forward",
        )
        # And at least one chunk WAS observed on disk (otherwise the
        # sampling itself is broken).
        self.assertGreater(peak[0], 0,
                           "peak sampling never observed any blob on disk")

        # Decrypt + assemble and verify byte-for-byte.
        assembled = b"".join(
            KeyManager.decrypt_chunk(ciphertext_by_index[i], fx.key)
            for i in range(chunk_count)
        )
        self.assertEqual(assembled, src_bytes)

        # Server storage dir is empty (or gone) after the last ack.
        storage = Path(self.h._server_copy) / "storage" / sent_tid
        if storage.exists():
            self.assertEqual(list(storage.iterdir()), [])


class StreamingAbortTests(unittest.TestCase):
    """Either-party abort-mid-stream invariants."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ServerHarness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def _send_with_abort_after_first_chunk(
            self, fx: _StreamingFixture, src: Path,
            abort_caller: str,
    ) -> tuple[str, list[tuple[int, str]]]:
        """Drive sender.send_file; after the first successful chunk
        callback, fire abort_transfer from the given caller side
        ('sender' or 'recipient'). Returns (tid, events). Sender's
        ``send_file`` should return None — the next chunk upload will
        see 410."""
        sender = fx.sender_api()
        recipient_api = fx.recipient_api()
        events: list[tuple[int, str]] = []
        tid_captured: list[str | None] = [None]
        abort_fired = [False]

        def on_stream_progress(tid, uploaded, total, state):
            tid_captured[0] = tid
            events.append((uploaded, state))
            if uploaded == 1 and state == "sending" and not abort_fired[0]:
                abort_fired[0] = True
                if abort_caller == "sender":
                    sender.abort_transfer(tid, "sender_abort")
                else:
                    recipient_api.abort_transfer(tid, "recipient_abort")

        sent_tid = sender.send_file(
            src, fx.recipient_id, fx.key,
            on_stream_progress=on_stream_progress,
            streaming=True,
        )
        self.assertIsNone(sent_tid,
                          f"send_file should have returned None, got {sent_tid}")
        self.assertIsNotNone(tid_captured[0])
        return tid_captured[0], events

    def test_sender_abort_midstream(self):
        fx = _StreamingFixture(self.h)
        src = fx.tmp / "payload.bin"
        src.write_bytes(b"S" * (3 * CHUNK_SIZE))
        tid, events = self._send_with_abort_after_first_chunk(
            fx, src, abort_caller="sender",
        )
        self.assertEqual(events[-1][1], "aborted")

        # Recipient's next GET → 410.
        outcome = fx.recipient_api().download_chunk(tid, 0)
        self.assertEqual(outcome.status, "aborted")

        # Poller picks up the 410 cleanly: row ends ABORTED, .part wiped.
        recv_history_dir = fx.tmp / "recv_hist_sa"
        recv_history_dir.mkdir()
        recv_save_dir = fx.tmp / "recv_save_sa"
        recv_save_dir.mkdir()
        poller = fx.build_recipient_poller(recv_history_dir, recv_save_dir)
        poller._receive_streaming_transfer(
            tid, fx.sender_id, "deadfile.bin", 3,
            os.urandom(24), fx.key,
        )
        [row] = TransferHistory(recv_history_dir).items
        self.assertEqual(row["status"], TransferStatus.ABORTED)
        # abort_reason should be sender_abort (reason was carried on
        # the 410), though we tolerate None in case the server's 410
        # body didn't include it.
        self.assertIn(row.get("abort_reason"), ("sender_abort", None))
        part = recv_save_dir / ".parts" / f".incoming_{tid}.part"
        self.assertFalse(part.exists(),
                         ".part should be cleaned after sender abort")

    def test_recipient_abort_midstream(self):
        fx = _StreamingFixture(self.h)
        src = fx.tmp / "payload.bin"
        src.write_bytes(b"R" * (3 * CHUNK_SIZE))
        tid, events = self._send_with_abort_after_first_chunk(
            fx, src, abort_caller="recipient",
        )
        self.assertEqual(events[-1][1], "aborted")

        # Recipient's subsequent GET also sees 410.
        outcome = fx.recipient_api().download_chunk(tid, 0)
        self.assertEqual(outcome.status, "aborted")

        # Drive a Poller too — the recipient side should behave
        # identically regardless of who initiated the abort.
        recv_history_dir = fx.tmp / "recv_hist_ra"
        recv_history_dir.mkdir()
        recv_save_dir = fx.tmp / "recv_save_ra"
        recv_save_dir.mkdir()
        poller = fx.build_recipient_poller(recv_history_dir, recv_save_dir)
        poller._receive_streaming_transfer(
            tid, fx.sender_id, "deadfile.bin", 3,
            os.urandom(24), fx.key,
        )
        [row] = TransferHistory(recv_history_dir).items
        self.assertEqual(row["status"], TransferStatus.ABORTED)
        part = recv_save_dir / ".parts" / f".incoming_{tid}.part"
        self.assertFalse(part.exists())


class StreamingQuotaGateTests(unittest.TestCase):
    """Tight server quota forces mid-stream 507; in-callback drain
    frees the slot and the sender recovers."""

    @classmethod
    def setUpClass(cls) -> None:
        # 3 MB quota: one chunk (2 MB) fits, a second chunk overshoots
        # because sumPending still counts the first until it's acked.
        cls.h = _ServerHarness(config_overrides={"storageQuotaMB": 3})
        cls.h.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def test_waiting_stream_flip_then_drain_recovers(self):
        fx = _StreamingFixture(self.h)

        chunk_count = 3
        src = fx.tmp / "payload.bin"
        src.write_bytes(b"Q" * (chunk_count * CHUNK_SIZE - 37))
        src_bytes = src.read_bytes()

        sender = fx.sender_api()
        recipient_api = fx.recipient_api()
        events: list[tuple[int, str]] = []
        drain_idx = [0]
        ciphertext_by_index: dict[int, bytes] = {}
        tid_holder: list[str | None] = [None]

        def drain_one(tid):
            """Pull and ack the next unacked chunk so the server's
            quota sum drops by one chunk's worth."""
            idx = drain_idx[0]
            outcome = recipient_api.download_chunk(tid, idx)
            if outcome.status != "ok":
                return False
            ciphertext_by_index[idx] = outcome.data
            self.assertTrue(recipient_api.ack_chunk(tid, idx))
            drain_idx[0] += 1
            return True

        def on_stream_progress(tid, uploaded, total, state):
            tid_holder[0] = tid
            events.append((uploaded, state))
            if state == "waiting_stream":
                # Free a chunk's worth of quota so the sender's next
                # retry succeeds. Called inline from the sender's
                # backoff loop — same thread.
                drain_one(tid)

        with patch.object(api_client_mod, "STREAM_QUOTA_BACKOFF_RAMP_S",
                          _FAST_QUOTA_RAMP):
            sent_tid = sender.send_file(
                src, fx.recipient_id, fx.key,
                on_stream_progress=on_stream_progress,
                streaming=True,
            )
        self.assertIsNotNone(sent_tid,
                             f"send_file failed; events={events}")

        # Drain any remaining chunks that weren't drained during
        # waiting_stream (the last chunk may stay on the server).
        while drain_idx[0] < chunk_count:
            self.assertTrue(drain_one(sent_tid),
                            f"leftover drain failed at idx={drain_idx[0]}")

        # The whole point: at least one waiting_stream event fired.
        states = [s for _, s in events]
        self.assertIn("waiting_stream", states,
                      f"never entered waiting_stream — quota gate "
                      f"didn't trigger. events={events}")
        # AND we recovered to 'sending' after the first waiting_stream.
        first_wait = states.index("waiting_stream")
        self.assertIn("sending", states[first_wait + 1:],
                      "no 'sending' after waiting_stream — sender "
                      "did not recover")

        # Content round-trips correctly despite the back-pressure.
        assembled = b"".join(
            KeyManager.decrypt_chunk(ciphertext_by_index[i], fx.key)
            for i in range(chunk_count)
        )
        self.assertEqual(assembled, src_bytes)

        # Server storage wiped.
        storage = Path(self.h._server_copy) / "storage" / sent_tid
        if storage.exists():
            self.assertEqual(list(storage.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
