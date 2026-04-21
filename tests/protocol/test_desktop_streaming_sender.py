"""Unit tests for the desktop streaming-sender state machine introduced
in Phase C.4.

Drives ``ApiClient._upload_stream_chunk`` and ``ApiClient._upload_stream``
with a mocked server so we can pin retry policy, 507 backoff, 410
handling, and progress-callback sequencing without HTTP.

End-to-end (streaming sender + hermetic server + streaming recipient)
is exercised by the hand-run block at the bottom of this phase's
commit message and formally by C.6.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT  # noqa: E402

sys.path.insert(0, REPO_ROOT)

from desktop.src import api_client as api_client_mod  # noqa: E402
from desktop.src.api_client import (  # noqa: E402
    ApiClient,
    ChunkUploadOutcome,
    STREAM_QUOTA_BACKOFF_RAMP_S,
    STORAGE_FULL_MAX_WINDOW_S,
    UPLOAD_ABORTED,
    UPLOAD_AUTH_ERROR,
    UPLOAD_NETWORK_ERROR,
    UPLOAD_OK,
    UPLOAD_STORAGE_FULL,
)
from desktop.src.crypto import CHUNK_SIZE  # noqa: E402


def _build_api() -> tuple[ApiClient, MagicMock]:
    """ApiClient with its ConnectionManager mocked. Only
    `clear_storage_full` / `mark_storage_full` / `device_id` / `auth_token`
    / `server_url` / `request` are touched in the sender paths."""
    conn = MagicMock()
    conn.server_url = "http://test.invalid"
    conn.device_id = "dev"
    conn.auth_token = "tok"
    crypto = MagicMock()
    api = ApiClient(conn, crypto)
    return api, conn


class UploadStreamChunkTests(unittest.TestCase):
    """_upload_stream_chunk is where the 507/410/network retry policies
    live. Each case mocks ApiClient.upload_chunk so we can inject the
    exact outcome sequence we want to test."""

    def setUp(self) -> None:
        self.api, self.conn = _build_api()

    def test_ok_first_try_no_sleep(self):
        with patch.object(self.api, "upload_chunk",
                          return_value=ChunkUploadOutcome(status=UPLOAD_OK,
                                                          body={"chunks_received": 1},
                                                          http_status=200)):
            with patch.object(api_client_mod.time, "sleep") as sleep_mock:
                result = self.api._upload_stream_chunk(
                    "tid", 0, 5, b"ct", None,
                )
        self.assertEqual(result, "ok")
        sleep_mock.assert_not_called()
        # Not waiting for quota → clear_storage_full should not have been called.
        self.conn.clear_storage_full.assert_not_called()

    def test_aborted_410_returns_immediately(self):
        with patch.object(self.api, "upload_chunk",
                          return_value=ChunkUploadOutcome(
                              status=UPLOAD_ABORTED,
                              abort_reason="recipient_abort",
                              http_status=410)):
            with patch.object(api_client_mod.time, "sleep") as sleep_mock:
                result = self.api._upload_stream_chunk(
                    "tid", 0, 5, b"ct", None,
                )
        self.assertEqual(result, "aborted")
        sleep_mock.assert_not_called()

    def test_507_then_ok_flips_through_waiting_stream(self):
        """Single 507 → caller fires state='waiting_stream' exactly once,
        sleeps per backoff ramp, retries and succeeds, then state
        transitions fire again from the enclosing _upload_stream (not
        here — this helper only fires waiting_stream on first 507)."""
        outcomes = [
            ChunkUploadOutcome(status=UPLOAD_STORAGE_FULL, http_status=507),
            ChunkUploadOutcome(status=UPLOAD_OK,
                               body={"chunks_received": 1},
                               http_status=200),
        ]
        on_progress = MagicMock()
        slept = []
        with patch.object(self.api, "upload_chunk", side_effect=outcomes):
            with patch.object(api_client_mod.time, "sleep",
                              side_effect=slept.append):
                result = self.api._upload_stream_chunk(
                    "tid", 0, 5, b"ct", on_progress,
                )
        self.assertEqual(result, "ok")
        # waiting_stream fired exactly once, with state string literal.
        on_progress.assert_called_once_with("tid", 0, 5, "waiting_stream")
        # First backoff step from the ramp.
        self.assertEqual(slept, [STREAM_QUOTA_BACKOFF_RAMP_S[0]])
        self.conn.mark_storage_full.assert_called_once()
        self.conn.clear_storage_full.assert_called_once()

    def test_507_budget_exhausted_returns_failed(self):
        """Continuous 507s past STORAGE_FULL_MAX_WINDOW_S → failed.
        Emulate the clock via monkey-patched time.monotonic so the 30-min
        budget trips after a few iterations instead of in real time."""
        on_progress = MagicMock()
        fake_now = [0.0]

        def fake_monotonic():
            return fake_now[0]

        def fake_sleep(secs):
            fake_now[0] += max(secs, 1.0)

        with patch.object(self.api, "upload_chunk",
                          return_value=ChunkUploadOutcome(
                              status=UPLOAD_STORAGE_FULL, http_status=507)):
            with patch.object(api_client_mod.time, "monotonic", fake_monotonic):
                with patch.object(api_client_mod.time, "sleep", fake_sleep):
                    result = self.api._upload_stream_chunk(
                        "tid", 1, 5, b"ct", on_progress,
                    )
        self.assertEqual(result, "failed")
        # Verify we actually spent >= STORAGE_FULL_MAX_WINDOW_S of
        # simulated time before giving up.
        self.assertGreaterEqual(fake_now[0], STORAGE_FULL_MAX_WINDOW_S)
        # waiting_stream fired exactly once (on the FIRST 507, not every 507).
        on_progress.assert_called_once_with("tid", 1, 5, "waiting_stream")

    def test_network_errors_exhaust_2min_budget(self):
        """Continuous non-507 errors past CHUNK_MAX_FAILURE_WINDOW_S
        (2 min) → failed. Same shape as classic upload budget."""
        fake_now = [0.0]

        def fake_monotonic():
            return fake_now[0]

        def fake_sleep(secs):
            fake_now[0] += max(secs, 1.0)

        with patch.object(self.api, "upload_chunk",
                          return_value=ChunkUploadOutcome(
                              status=UPLOAD_NETWORK_ERROR)):
            with patch.object(api_client_mod.time, "monotonic", fake_monotonic):
                with patch.object(api_client_mod.time, "sleep", fake_sleep):
                    result = self.api._upload_stream_chunk(
                        "tid", 0, 5, b"ct", None,
                    )
        self.assertEqual(result, "failed")

    def test_network_flake_recovers(self):
        """One network error followed by OK: no abort, returns ok."""
        outcomes = [
            ChunkUploadOutcome(status=UPLOAD_NETWORK_ERROR),
            ChunkUploadOutcome(status=UPLOAD_OK,
                               body={"chunks_received": 1},
                               http_status=200),
        ]
        with patch.object(self.api, "upload_chunk", side_effect=outcomes):
            with patch.object(api_client_mod.time, "sleep"):
                result = self.api._upload_stream_chunk(
                    "tid", 0, 5, b"ct", None,
                )
        self.assertEqual(result, "ok")

    def test_mixed_507_and_network_keep_separate_budgets(self):
        """507 → network_error → 507 → OK. The 2-min network budget
        must not be confused with the 30-min quota budget."""
        outcomes = [
            ChunkUploadOutcome(status=UPLOAD_STORAGE_FULL, http_status=507),
            ChunkUploadOutcome(status=UPLOAD_NETWORK_ERROR),
            ChunkUploadOutcome(status=UPLOAD_STORAGE_FULL, http_status=507),
            ChunkUploadOutcome(status=UPLOAD_OK,
                               body={"chunks_received": 1},
                               http_status=200),
        ]
        on_progress = MagicMock()
        with patch.object(self.api, "upload_chunk", side_effect=outcomes):
            with patch.object(api_client_mod.time, "sleep"):
                result = self.api._upload_stream_chunk(
                    "tid", 0, 5, b"ct", on_progress,
                )
        self.assertEqual(result, "ok")
        # waiting_stream still fires exactly once — on the first 507.
        on_progress.assert_called_once_with("tid", 0, 5, "waiting_stream")


class UploadStreamPipelineTests(unittest.TestCase):
    """_upload_stream ties _upload_stream_chunk to the file reader,
    encryption, and progress callback. These tests pin the orchestration
    without touching HTTP."""

    def setUp(self) -> None:
        self.api, self.conn = _build_api()
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-stream-send-"))
        self.filepath = self.tmp / "payload.bin"
        # 3 KB of deterministic bytes — well under one CHUNK_SIZE so
        # the uploader sees exactly one chunk. We mock encrypt_chunk
        # downstream so the actual crypto isn't exercised here.
        self.filepath.write_bytes(b"A" * 3072)

    def test_happy_path_three_chunks_reports_sending_progress(self):
        # Simulate a 3-chunk transfer regardless of file size by
        # passing chunk_count=3; the file is short enough that read()
        # returns b'' on chunks 1 and 2, which encrypt_chunk still
        # accepts.
        on_progress = MagicMock()
        with patch.object(self.api, "_upload_stream_chunk",
                          return_value="ok") as chunk_mock:
            with patch.object(api_client_mod.KeyManager, "encrypt_chunk",
                              return_value=b"ct"):
                tid = self.api._upload_stream(
                    self.filepath, "tid-happy", 3,
                    b"n" * 24, b"k" * 32, on_progress,
                )
        self.assertEqual(tid, "tid-happy")
        self.assertEqual(chunk_mock.call_count, 3)
        # Progress: initial (0,'sending') + three post-chunk (1,2,3,
        # 'sending'). No 'waiting_stream', no 'aborted', no 'failed'.
        states = [c.args[3] for c in on_progress.call_args_list]
        self.assertEqual(states, ["sending", "sending", "sending", "sending"])
        # Counts strictly monotone 0 → 1 → 2 → 3.
        counts = [c.args[1] for c in on_progress.call_args_list]
        self.assertEqual(counts, [0, 1, 2, 3])

    def test_recipient_abort_stops_upload_no_delete(self):
        """_upload_stream returns None when the chunk helper reports
        'aborted', and does NOT call abort_transfer (server already
        wiped the blobs when it emitted 410)."""
        on_progress = MagicMock()
        with patch.object(self.api, "_upload_stream_chunk",
                          return_value="aborted"):
            with patch.object(self.api, "abort_transfer") as abort_mock:
                with patch.object(api_client_mod.KeyManager, "encrypt_chunk",
                                  return_value=b"ct"):
                    tid = self.api._upload_stream(
                        self.filepath, "tid-aborted", 3,
                        b"n" * 24, b"k" * 32, on_progress,
                    )
        self.assertIsNone(tid)
        abort_mock.assert_not_called()
        # Last callback carries state='aborted'.
        self.assertEqual(on_progress.call_args_list[-1].args[3], "aborted")

    def test_sender_side_failure_sends_sender_failed(self):
        """Chunk helper returns 'failed' → _upload_stream must DELETE
        with reason=sender_failed so the recipient's row flips to
        aborted, then fire state='failed' on the callback."""
        on_progress = MagicMock()
        with patch.object(self.api, "_upload_stream_chunk",
                          return_value="failed"):
            with patch.object(self.api, "abort_transfer",
                              return_value=True) as abort_mock:
                with patch.object(api_client_mod.KeyManager, "encrypt_chunk",
                                  return_value=b"ct"):
                    tid = self.api._upload_stream(
                        self.filepath, "tid-failed", 3,
                        b"n" * 24, b"k" * 32, on_progress,
                    )
        self.assertIsNone(tid)
        abort_mock.assert_called_once_with("tid-failed", "sender_failed")
        self.assertEqual(on_progress.call_args_list[-1].args[3], "failed")


class SendFileModeNegotiationTests(unittest.TestCase):
    """send_file's entry-point logic that picks requested_mode and
    routes to _upload_stream vs the classic loop."""

    def setUp(self) -> None:
        self.api, self.conn = _build_api()
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-mode-"))
        self.filepath = self.tmp / "payload.bin"
        self.filepath.write_bytes(b"X" * 1024)

    def _common_patches(self, *, supports_streaming: bool, negotiated: str):
        # Stub everything downstream of the routing decision so we can
        # assert which branch was taken.
        return {
            "supports_streaming": patch.object(self.api,
                                                "supports_streaming",
                                                return_value=supports_streaming),
            "init_with_retry": patch.object(self.api,
                                             "_init_transfer_with_retry",
                                             return_value=negotiated),
            "upload_stream": patch.object(self.api, "_upload_stream",
                                           return_value="tid-ok"),
            "upload_chunk_retry": patch.object(self.api,
                                                "_upload_chunk_with_retry",
                                                return_value=None),
        }

    def test_streaming_requested_when_server_supports(self):
        patches = self._common_patches(supports_streaming=True,
                                        negotiated="streaming")
        with patches["supports_streaming"], patches["init_with_retry"] as init, \
             patches["upload_stream"] as stream, patches["upload_chunk_retry"] as classic:
            self.api.send_file(self.filepath, "recipient", b"k" * 32)
        # Check the requested mode at init time.
        self.assertEqual(init.call_args.kwargs.get("mode"), "streaming")
        stream.assert_called_once()
        classic.assert_not_called()

    def test_classic_requested_when_server_lacks_capability(self):
        patches = self._common_patches(supports_streaming=False,
                                        negotiated="classic")
        with patches["supports_streaming"], patches["init_with_retry"] as init, \
             patches["upload_stream"] as stream, patches["upload_chunk_retry"] as classic:
            self.api.send_file(self.filepath, "recipient", b"k" * 32)
        self.assertEqual(init.call_args.kwargs.get("mode"), "classic")
        classic.assert_called()
        stream.assert_not_called()

    def test_streaming_disabled_via_flag(self):
        """streaming=False forces classic even when the server supports
        streaming. Used by callers that want a guaranteed classic
        transfer (testing, targeted compat)."""
        patches = self._common_patches(supports_streaming=True,
                                        negotiated="classic")
        with patches["supports_streaming"], patches["init_with_retry"] as init, \
             patches["upload_stream"] as stream, patches["upload_chunk_retry"] as classic:
            self.api.send_file(self.filepath, "recipient", b"k" * 32,
                               streaming=False)
        self.assertEqual(init.call_args.kwargs.get("mode"), "classic")
        classic.assert_called()
        stream.assert_not_called()

    def test_fn_command_always_classic(self):
        """.fn.* commands skip streaming even when the server advertises
        capability — too small to benefit, extra round-trips hurt."""
        fn_path = self.tmp / ".fn.clipboard.text"
        fn_path.write_bytes(b"hello")
        patches = self._common_patches(supports_streaming=True,
                                        negotiated="classic")
        with patches["supports_streaming"], patches["init_with_retry"] as init, \
             patches["upload_stream"] as stream, patches["upload_chunk_retry"] as classic:
            self.api.send_file(fn_path, "recipient", b"k" * 32)
        self.assertEqual(init.call_args.kwargs.get("mode"), "classic")
        classic.assert_called()
        stream.assert_not_called()

    def test_server_downgrade_runs_classic_body(self):
        """Client requests streaming, server downgrades to classic
        (recipient offline / streamingEnabled=false). The POST-init
        branch must take the classic path even though requested_mode
        was 'streaming'."""
        patches = self._common_patches(supports_streaming=True,
                                        negotiated="classic")
        with patches["supports_streaming"], patches["init_with_retry"] as init, \
             patches["upload_stream"] as stream, patches["upload_chunk_retry"] as classic:
            self.api.send_file(self.filepath, "recipient", b"k" * 32)
        self.assertEqual(init.call_args.kwargs.get("mode"), "streaming")
        # But the actual upload ran on the classic helper.
        stream.assert_not_called()
        classic.assert_called()


if __name__ == "__main__":
    unittest.main()
