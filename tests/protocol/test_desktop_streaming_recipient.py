"""Unit tests for the desktop streaming-recipient helpers introduced in
Phase C.3.

These tests drive ``Poller._stream_download_chunk`` and
``Poller._receive_streaming_transfer`` with a mocked ``ApiClient`` so
we can assert retry policy, budget enforcement, ack sequencing, and
file finalization without touching HTTP. The full end-to-end
(streaming sender + hermetic server + recipient) test is C.6.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

sys.path.insert(0, REPO_ROOT)

from desktop.src.api_client import (  # noqa: E402
    ChunkDownloadOutcome,
    DOWNLOAD_ABORTED,
    DOWNLOAD_NETWORK_ERROR,
    DOWNLOAD_OK,
    DOWNLOAD_TOO_EARLY,
)
from desktop.src.crypto import KeyManager, CHUNK_SIZE  # noqa: E402
from desktop.src.history import TransferHistory, TransferStatus  # noqa: E402
from desktop.src import poller as poller_mod  # noqa: E402
from desktop.src.poller import Poller  # noqa: E402


def _build_poller(tmp_root: Path, history: TransferHistory,
                  api_mock: MagicMock) -> Poller:
    """Build a Poller with a stub config + platform so the helper
    methods are callable without a running tray / server. We only
    exercise ``_stream_download_chunk`` and ``_receive_streaming_transfer``;
    nothing in those paths touches the connection / tracker loops.
    """
    config = MagicMock()
    config.config_dir = tmp_root / "config"
    config.config_dir.mkdir(parents=True, exist_ok=True)
    # save_directory is a @property in the real Config; our mock just
    # returns a plain Path — the recipient mkdirs .parts/ under it.
    config.save_directory = tmp_root / "save"
    config.save_directory.mkdir(parents=True, exist_ok=True)
    config.paired_devices = {}

    crypto = MagicMock()
    conn = MagicMock()
    conn.state = None  # never read in the streaming paths we test
    platform = MagicMock()

    return Poller(
        config=config,
        connection=conn,
        api=api_mock,
        crypto=crypto,
        history=history,
        platform=platform,
    )


# ---- _stream_download_chunk: retry / abort / failure policy -----------


class StreamDownloadChunkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-stream-helper-"))
        self.history = TransferHistory(self.tmp)
        self.api = MagicMock()
        self.poller = _build_poller(self.tmp, self.history, self.api)
        # Real symmetric key so decrypt paths succeed when we feed real
        # ciphertext; helper tests that don't exercise decrypt can pass
        # any 32 bytes and stub decrypt_chunk separately.
        self.key = os.urandom(32)

    def test_ok_returns_plaintext_no_sleep(self):
        """A 200 on the first try short-circuits all retry logic."""
        encrypted = KeyManager.encrypt_chunk(b"plain-0", os.urandom(24), 0, self.key)
        self.api.download_chunk.return_value = ChunkDownloadOutcome(
            status=DOWNLOAD_OK, data=encrypted, http_status=200,
        )
        with patch.object(poller_mod.time, "sleep") as sleep_mock:
            with patch.object(KeyManager, "decrypt_chunk", return_value=b"plain-0"):
                state, payload = self.poller._stream_download_chunk(
                    "tid", 0, 3, self.key,
                )
        self.assertEqual(state, "ok")
        self.assertEqual(payload, b"plain-0")
        sleep_mock.assert_not_called()

    def test_too_early_retries_then_succeeds(self):
        """425 → wait (server hint) → 200 flows through cleanly."""
        self.api.download_chunk.side_effect = [
            ChunkDownloadOutcome(status=DOWNLOAD_TOO_EARLY,
                                 retry_after_ms=500, http_status=425),
            ChunkDownloadOutcome(status=DOWNLOAD_TOO_EARLY,
                                 retry_after_ms=1000, http_status=425),
            ChunkDownloadOutcome(status=DOWNLOAD_OK,
                                 data=b"ct", http_status=200),
        ]
        slept = []
        with patch.object(poller_mod.time, "sleep",
                          side_effect=slept.append):
            with patch.object(KeyManager, "decrypt_chunk",
                              return_value=b"plain"):
                state, payload = self.poller._stream_download_chunk(
                    "tid", 1, 3, self.key,
                )
        self.assertEqual(state, "ok")
        self.assertEqual(payload, b"plain")
        # Two 425s → two sleeps; hints stay within ramp caps.
        self.assertEqual(len(slept), 2)
        # First wait clamped to min 0.5 s floor even though hint was 0.5 s.
        self.assertGreaterEqual(slept[0], 0.5)

    def test_too_early_budget_exhausted(self):
        """Continuous 425s past STREAM_CHUNK_WAIT_BUDGET_S → failed."""
        # Keep returning 425 forever.
        self.api.download_chunk.return_value = ChunkDownloadOutcome(
            status=DOWNLOAD_TOO_EARLY, retry_after_ms=1000, http_status=425,
        )
        fake_now = [0.0]

        def fake_monotonic():
            return fake_now[0]

        def fake_sleep(secs):
            # Each "sleep" advances our fake clock so the budget check
            # eventually trips.
            fake_now[0] += max(secs, 1.0)

        with patch.object(poller_mod.time, "monotonic", fake_monotonic):
            with patch.object(poller_mod.time, "sleep", fake_sleep):
                state, payload = self.poller._stream_download_chunk(
                    "tid", 2, 3, self.key,
                )
        self.assertEqual(state, "failed")
        # payload is a human string — pin the shape, not the exact text.
        self.assertIn("upstream", str(payload))

    def test_aborted_returns_reason(self):
        """410 → ('aborted', abort_reason) in one round trip, no sleep."""
        self.api.download_chunk.return_value = ChunkDownloadOutcome(
            status=DOWNLOAD_ABORTED, abort_reason="sender_abort",
            http_status=410,
        )
        with patch.object(poller_mod.time, "sleep") as sleep_mock:
            state, payload = self.poller._stream_download_chunk(
                "tid", 0, 3, self.key,
            )
        self.assertEqual(state, "aborted")
        self.assertEqual(payload, "sender_abort")
        sleep_mock.assert_not_called()

    def test_aborted_without_reason(self):
        """410 with no body field → abort_reason=None propagates through."""
        self.api.download_chunk.return_value = ChunkDownloadOutcome(
            status=DOWNLOAD_ABORTED, abort_reason=None, http_status=410,
        )
        state, payload = self.poller._stream_download_chunk(
            "tid", 0, 3, self.key,
        )
        self.assertEqual(state, "aborted")
        self.assertIsNone(payload)

    def test_network_errors_retry_then_fail(self):
        """3 consecutive network errors exhaust the budget → failed."""
        self.api.download_chunk.return_value = ChunkDownloadOutcome(
            status=DOWNLOAD_NETWORK_ERROR, http_status=None,
        )
        slept = []
        with patch.object(poller_mod.time, "sleep",
                          side_effect=slept.append):
            state, payload = self.poller._stream_download_chunk(
                "tid", 0, 3, self.key,
            )
        self.assertEqual(state, "failed")
        self.assertIn("network_error", str(payload))
        # Matches classic helper: 3 attempts with 2×attempt backoff
        # (but the last attempt doesn't sleep after itself — we return
        # as soon as the counter trips). Sleeps fire between attempts.
        self.assertEqual(len(slept), 2)

    def test_network_error_recovers_on_retry(self):
        """Transient network blip followed by 200 → ok, no abort."""
        encrypted = b"cipher"
        self.api.download_chunk.side_effect = [
            ChunkDownloadOutcome(status=DOWNLOAD_NETWORK_ERROR, http_status=None),
            ChunkDownloadOutcome(status=DOWNLOAD_OK, data=encrypted,
                                 http_status=200),
        ]
        with patch.object(poller_mod.time, "sleep"):
            with patch.object(KeyManager, "decrypt_chunk",
                              return_value=b"plain"):
                state, payload = self.poller._stream_download_chunk(
                    "tid", 0, 3, self.key,
                )
        self.assertEqual(state, "ok")
        self.assertEqual(payload, b"plain")

    def test_425_then_network_error_resets_budget(self):
        """A non-425 response must reset the 425 streak — otherwise a
        mix of quick 425/network hiccups could falsely trip the dead-
        upstream budget."""
        self.api.download_chunk.side_effect = [
            ChunkDownloadOutcome(status=DOWNLOAD_TOO_EARLY,
                                 retry_after_ms=1000, http_status=425),
            ChunkDownloadOutcome(status=DOWNLOAD_NETWORK_ERROR, http_status=None),
            ChunkDownloadOutcome(status=DOWNLOAD_TOO_EARLY,
                                 retry_after_ms=1000, http_status=425),
            ChunkDownloadOutcome(status=DOWNLOAD_OK, data=b"ct",
                                 http_status=200),
        ]
        with patch.object(poller_mod.time, "sleep"):
            with patch.object(KeyManager, "decrypt_chunk",
                              return_value=b"plain"):
                state, payload = self.poller._stream_download_chunk(
                    "tid", 0, 3, self.key,
                )
        self.assertEqual(state, "ok")


# ---- _receive_streaming_transfer: end-to-end with mocked API ----------


class ReceiveStreamingTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-stream-recv-"))
        self.history = TransferHistory(self.tmp)
        self.api = MagicMock()
        self.poller = _build_poller(self.tmp, self.history, self.api)
        self.key = os.urandom(32)
        self.base_nonce = os.urandom(24)
        self.transfer_id = "tid-recv-1"
        self.sender_id = "sender-1"

    def _make_chunk(self, payload: bytes, index: int) -> tuple[bytes, bytes]:
        """Return (plaintext, ciphertext) for an index."""
        ct = KeyManager.encrypt_chunk(payload, self.base_nonce, index, self.key)
        return payload, ct

    def test_happy_path_three_chunks(self):
        pt0, ct0 = self._make_chunk(b"A" * 16, 0)
        pt1, ct1 = self._make_chunk(b"B" * 16, 1)
        pt2, ct2 = self._make_chunk(b"C" * 16, 2)
        self.api.download_chunk.side_effect = [
            ChunkDownloadOutcome(status=DOWNLOAD_OK, data=ct0, http_status=200),
            ChunkDownloadOutcome(status=DOWNLOAD_OK, data=ct1, http_status=200),
            ChunkDownloadOutcome(status=DOWNLOAD_OK, data=ct2, http_status=200),
        ]
        self.api.ack_chunk.return_value = True

        with patch.object(poller_mod.time, "sleep"):
            self.poller._receive_streaming_transfer(
                self.transfer_id, self.sender_id, "hello.txt", 3,
                self.base_nonce, self.key,
            )

        # Per-chunk ack fired in order, NO transfer-level ack.
        self.assertEqual(
            self.api.ack_chunk.call_args_list,
            [((self.transfer_id, 0),), ((self.transfer_id, 1),),
             ((self.transfer_id, 2),)],
        )
        self.api.ack_transfer.assert_not_called()
        self.api.abort_transfer.assert_not_called()

        # Final file written and matches the concatenated plaintexts.
        [row] = [it for it in self.history.items
                 if it["transfer_id"] == self.transfer_id]
        self.assertEqual(row["status"], TransferStatus.COMPLETE)
        self.assertTrue(row["delivered"])
        final = Path(row["content_path"])
        self.assertTrue(final.exists())
        self.assertEqual(final.read_bytes(), pt0 + pt1 + pt2)
        # .part cleaned up
        self.assertFalse(
            (self.tmp / "save" / ".parts" /
             f".incoming_{self.transfer_id}.part").exists()
        )

    def test_sender_abort_midstream(self):
        """Chunk 0 OK, chunk 1 returns 410 — row flips to aborted,
        .part cleaned, NO abort_transfer DELETE sent (server already
        wiped its blobs)."""
        _, ct0 = self._make_chunk(b"A" * 16, 0)
        self.api.download_chunk.side_effect = [
            ChunkDownloadOutcome(status=DOWNLOAD_OK, data=ct0, http_status=200),
            ChunkDownloadOutcome(status=DOWNLOAD_ABORTED,
                                 abort_reason="sender_abort",
                                 http_status=410),
        ]
        self.api.ack_chunk.return_value = True

        with patch.object(poller_mod.time, "sleep"):
            self.poller._receive_streaming_transfer(
                self.transfer_id, self.sender_id, "dead.txt", 3,
                self.base_nonce, self.key,
            )

        # Only chunk 0's ack fired.
        self.api.ack_chunk.assert_called_once_with(self.transfer_id, 0)
        # Recipient did NOT DELETE — the server already aborted from
        # the sender side.
        self.api.abort_transfer.assert_not_called()

        [row] = [it for it in self.history.items
                 if it["transfer_id"] == self.transfer_id]
        self.assertEqual(row["status"], TransferStatus.ABORTED)
        self.assertEqual(row["abort_reason"], "sender_abort")
        # No file saved, .part cleaned.
        self.assertFalse(
            (self.tmp / "save" / ".parts" /
             f".incoming_{self.transfer_id}.part").exists()
        )

    def test_network_exhaustion_triggers_recipient_abort(self):
        """Network budget blown → history failed AND DELETE sent with
        reason=recipient_abort so the sender sees the dead stream."""
        self.api.download_chunk.return_value = ChunkDownloadOutcome(
            status=DOWNLOAD_NETWORK_ERROR, http_status=None,
        )
        self.api.abort_transfer.return_value = True

        with patch.object(poller_mod.time, "sleep"):
            self.poller._receive_streaming_transfer(
                self.transfer_id, self.sender_id, "doomed.txt", 2,
                self.base_nonce, self.key,
            )

        self.api.abort_transfer.assert_called_once_with(
            self.transfer_id, "recipient_abort",
        )
        [row] = [it for it in self.history.items
                 if it["transfer_id"] == self.transfer_id]
        self.assertEqual(row["status"], TransferStatus.FAILED)

    def test_ack_chunk_failure_triggers_recipient_abort(self):
        """If the server rejects our ack (shouldn't happen in practice,
        but we defend against network blips), abort cleanly and mark
        the row failed rather than leaving it half-acked."""
        _, ct0 = self._make_chunk(b"A" * 16, 0)
        self.api.download_chunk.return_value = ChunkDownloadOutcome(
            status=DOWNLOAD_OK, data=ct0, http_status=200,
        )
        # First chunk ack fails.
        self.api.ack_chunk.return_value = False
        self.api.abort_transfer.return_value = True

        with patch.object(poller_mod.time, "sleep"):
            self.poller._receive_streaming_transfer(
                self.transfer_id, self.sender_id, "nak.txt", 3,
                self.base_nonce, self.key,
            )

        self.api.abort_transfer.assert_called_once_with(
            self.transfer_id, "recipient_abort",
        )
        [row] = [it for it in self.history.items
                 if it["transfer_id"] == self.transfer_id]
        self.assertEqual(row["status"], TransferStatus.FAILED)

    def test_history_row_mode_streaming(self):
        """The row written at init time tags mode=streaming so the
        renderer picks the streaming status branches."""
        _, ct0 = self._make_chunk(b"A" * 16, 0)
        self.api.download_chunk.return_value = ChunkDownloadOutcome(
            status=DOWNLOAD_OK, data=ct0, http_status=200,
        )
        self.api.ack_chunk.return_value = True
        with patch.object(poller_mod.time, "sleep"):
            self.poller._receive_streaming_transfer(
                self.transfer_id, self.sender_id, "m.txt", 1,
                self.base_nonce, self.key,
            )
        [row] = [it for it in self.history.items
                 if it["transfer_id"] == self.transfer_id]
        self.assertEqual(row["mode"], "streaming")


# ---- _download_transfer mode routing ----------------------------------


class DownloadTransferModeRoutingTests(unittest.TestCase):
    """Pin that _download_transfer branches on the pending-list `mode`
    field so a classic server (no mode field) keeps taking the classic
    path and a streaming server's rows reach _receive_streaming_transfer.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-stream-route-"))
        self.history = TransferHistory(self.tmp)
        self.api = MagicMock()
        self.poller = _build_poller(self.tmp, self.history, self.api)

        # Set up a paired sender entry so metadata decrypt succeeds.
        self.sender_id = "sender-route"
        self.key = os.urandom(32)
        self.poller.config.paired_devices = {
            self.sender_id: {
                "symmetric_key_b64": base64.b64encode(self.key).decode(),
            }
        }
        # crypto.decrypt_metadata returns a dict we control.
        self.poller.crypto.decrypt_metadata.return_value = {
            "filename": "route.txt",
            "base_nonce": base64.b64encode(os.urandom(24)).decode(),
        }

    def _transfer_row(self, *, mode: str | None) -> dict:
        row = {
            "transfer_id": "tid-route",
            "sender_id": self.sender_id,
            "encrypted_meta": "e30=",
            "chunk_count": 2,
        }
        if mode is not None:
            row["mode"] = mode
        return row

    def test_streaming_mode_routes_to_streaming_handler(self):
        with patch.object(self.poller, "_receive_streaming_transfer") as ss, \
             patch.object(self.poller, "_receive_file_transfer") as cs:
            self.poller._download_transfer(self._transfer_row(mode="streaming"))
        ss.assert_called_once()
        cs.assert_not_called()

    def test_classic_mode_routes_to_classic_handler(self):
        with patch.object(self.poller, "_receive_streaming_transfer") as ss, \
             patch.object(self.poller, "_receive_file_transfer") as cs:
            self.poller._download_transfer(self._transfer_row(mode="classic"))
        cs.assert_called_once()
        ss.assert_not_called()

    def test_missing_mode_defaults_to_classic(self):
        """Old server that doesn't surface `mode` → classic path."""
        with patch.object(self.poller, "_receive_streaming_transfer") as ss, \
             patch.object(self.poller, "_receive_file_transfer") as cs:
            self.poller._download_transfer(self._transfer_row(mode=None))
        cs.assert_called_once()
        ss.assert_not_called()

    def test_fn_always_classic_even_if_mode_streaming(self):
        """`.fn.*` transfers skip the streaming path regardless of the
        server's negotiated mode — see streaming-improvement.md §9."""
        self.poller.crypto.decrypt_metadata.return_value = {
            "filename": ".fn.clipboard.text",
            "base_nonce": base64.b64encode(os.urandom(24)).decode(),
        }
        with patch.object(self.poller, "_receive_streaming_transfer") as ss, \
             patch.object(self.poller, "_receive_fn_transfer") as fn, \
             patch.object(self.poller, "_receive_file_transfer") as cs:
            self.poller._download_transfer(self._transfer_row(mode="streaming"))
        fn.assert_called_once()
        ss.assert_not_called()
        cs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
