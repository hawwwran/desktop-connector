-- Desktop Connector: streaming-relay schema extensions.
--
-- See docs/plans/streaming-improvement.md for the full design. Every
-- column is additive with safe defaults; classic transfers created by
-- old clients read back identically to pre-streaming behaviour.
--
-- Note: `chunks_downloaded` (from 001_initial.sql) keeps its classic
-- meaning in this migration ("recipient progress, final ACK only on
-- chunk_count"). In streaming mode it means "highest index + 1 the
-- recipient has per-chunk ACK'd" — same invariant with a more
-- granular ACK source.

-- Negotiated transport for this transfer: 'classic' (today's
-- store-then-forward) or 'streaming' (per-chunk pass-through + per-
-- chunk ACK + blob deletion on ACK).
ALTER TABLE transfers ADD COLUMN mode TEXT NOT NULL DEFAULT 'classic';

-- Terminal abort flag. Set once by TransferService::abort; wipes chunk
-- blobs. After aborted=1, every GET/POST/DELETE on this transfer 410s.
ALTER TABLE transfers ADD COLUMN aborted INTEGER NOT NULL DEFAULT 0;

-- One of 'recipient_abort' | 'sender_abort' | 'sender_failed'.
-- Sender and recipient render the row differently by inspecting this.
ALTER TABLE transfers ADD COLUMN abort_reason TEXT;

-- Unix timestamp of the abort transition. 0/NULL when aborted=0.
ALTER TABLE transfers ADD COLUMN aborted_at INTEGER;

-- Timestamp of the first chunk storing event for a streaming transfer
-- (fires the `stream_ready` FCM wake exactly once). 0/NULL for classic.
ALTER TABLE transfers ADD COLUMN stream_ready_at INTEGER;

-- Bumped every time a chunk is served. Reserved for a future streaming
-- stall-safeguard; written but not yet read.
ALTER TABLE transfers ADD COLUMN last_served_at INTEGER;

-- Streaming-only counter. Tracks how many chunks the server has
-- actually stored (distinct from `chunks_received`, which in classic
-- mode moves in lockstep with uploads too but semantically means
-- "recipient can start downloading"). For streaming the sender's UI
-- labels "Sending X→Y" with X=chunks_uploaded, Y=chunks_downloaded.
ALTER TABLE transfers ADD COLUMN chunks_uploaded INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_transfers_aborted ON transfers(aborted);
