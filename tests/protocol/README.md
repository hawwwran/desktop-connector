# Protocol contract tests

These tests pin the externally observable behavior of the protocol:
- **`test_desktop_message_contract.py`** — validates `FnTransferAdapter` and `FasttrackAdapter` message translations (in-process).
- **`test_server_contract.py`** — spins up a real PHP server on a random port and exercises registration, pairing, transfers, sent-status, long-poll notify, fasttrack, and the error-envelope contract (auth, validation, not-found, path-traversal).

They complement, not replace, `docs/plans/protocol.md` and `docs/protocol.examples.md`.

## Running

From the repo root:

```bash
python3 -m unittest discover tests/protocol
```

Or explicitly:

```bash
python3 -m unittest tests.protocol.test_desktop_message_contract
python3 -m unittest tests.protocol.test_server_contract
```

## Prerequisites

- Python 3.10+ (tests use `str | None` syntax).
- `php` on PATH (the server suite spawns `php -S`).
- No extra Python packages — only the standard library.

## What's hermetic and what isn't

- The server test copies `server/source` (without `data/`, `storage/`, or dotfiles) to a fresh tempdir per `setUpClass`, so it never touches the repo's SQLite DB or chunk storage.
- The server is shared across tests in the same class, but each test method registers fresh device credentials, so state from one test doesn't poison another's assertions.
- Tempdir and PHP subprocess are cleaned up in `tearDownClass` / `atexit`.

## Adding new contract assertions

1. If you're codifying a happy-path protocol example, add both the example to `docs/protocol.examples.md` and the assertion here.
2. If you're codifying an error-envelope shape, prefer `test_server_contract.ServerProtocolContractTests` — the harness is reusable.
3. Keep assertions on **observable behavior**, not implementation details. See `docs/protocol.compatibility.md` for the preserving / extending / breaking classification.
