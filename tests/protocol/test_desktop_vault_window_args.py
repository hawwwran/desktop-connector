"""F-U14: vault subprocess windows accept ``--vault-id``.

The dispatcher (`src.windows`) parses an optional ``--vault-id`` and
threads the normalized form into every ``show_vault_*`` entry point.
This file covers the two helpers that own the parsing + fallback
contract:

- :func:`vault.ui.window_args.parse_vault_id_arg` — strict normalizer
  that the dispatcher hands the raw CLI string to.
- :func:`vault.ui.window_args.resolve_active_vault_id` — the small
  router each window's ``local_vault_id()`` closure delegates to.

Source-pin coverage of the dispatcher wiring + per-window signature
lives in ``test_desktop_vault_browser_source.py``,
``test_desktop_vault_import_wizard_source.py`` and the new pins in
``test_desktop_vault_a11y_source.py``-style file
``test_desktop_vault_window_args_source.py`` (added below).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import REPO_ROOT, ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.config import Config  # noqa: E402
from src.vault.ui.window_args import (  # noqa: E402
    parse_vault_id_arg,
    resolve_active_vault_id,
)


VAULT_ID = "ABCD2345WXYZ"
OTHER_VAULT_ID = "MNOPQRSTUVWX"


class ParseVaultIdArgTests(unittest.TestCase):
    """The strict normalizer — invalid input must raise so the
    dispatcher can convert into an argparse error rather than letting
    the subprocess open against the wrong vault."""

    def test_none_passes_through(self) -> None:
        self.assertIsNone(parse_vault_id_arg(None))

    def test_empty_string_passes_through(self) -> None:
        # Empty after strip — falls back to last_known_id at the
        # window layer, same as not passing the flag at all.
        self.assertIsNone(parse_vault_id_arg(""))
        self.assertIsNone(parse_vault_id_arg("   "))
        self.assertIsNone(parse_vault_id_arg("---"))

    def test_canonical_form_passes_through(self) -> None:
        self.assertEqual(parse_vault_id_arg(VAULT_ID), VAULT_ID)

    def test_dashed_form_normalized(self) -> None:
        self.assertEqual(
            parse_vault_id_arg("ABCD-2345-WXYZ"),
            VAULT_ID,
        )

    def test_lowercase_form_normalized(self) -> None:
        self.assertEqual(
            parse_vault_id_arg("abcd2345wxyz"),
            VAULT_ID,
        )

    def test_mixed_case_dashed_form_normalized(self) -> None:
        self.assertEqual(
            parse_vault_id_arg("aBcD-2345-wXyZ"),
            VAULT_ID,
        )

    def test_short_form_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_vault_id_arg("ABCD2345")
        self.assertIn("12 base32", str(ctx.exception))

    def test_long_form_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_vault_id_arg("ABCD2345WXYZQ")
        self.assertIn("12 base32", str(ctx.exception))

    def test_non_base32_letters_rejected(self) -> None:
        # 1, 0, 8, 9 are not in the base32 alphabet.
        with self.assertRaises(ValueError) as ctx:
            parse_vault_id_arg("ABCD0123WXYZ")
        self.assertIn("non-base32", str(ctx.exception))

    def test_non_alphanumeric_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_vault_id_arg("ABCD!345WXYZ")
        self.assertIn("non-base32", str(ctx.exception))

    def test_whitespace_padded_form_normalized(self) -> None:
        # Argparse tends to leave leading/trailing whitespace alone;
        # strip so a copy-pasted id with a stray newline still works.
        self.assertEqual(
            parse_vault_id_arg("  ABCD-2345-WXYZ\n"),
            VAULT_ID,
        )


class ResolveActiveVaultIdTests(unittest.TestCase):
    """The fallback router — explicit override wins, otherwise read a
    fresh ``last_known_id`` off disk so the wizard's writes show up
    across subprocess boundaries."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-window-args-"))
        self.config = Config(self.tmp)

    def _write_last_known_id(self, vault_id: str) -> None:
        cfg_path = self.tmp / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text())
        else:
            data = {}
        data["vault"] = {"last_known_id": vault_id}
        cfg_path.write_text(json.dumps(data))

    def test_override_wins_over_last_known_id(self) -> None:
        # Stash a different id on disk; the override must still pin the
        # active vault to its own value.
        self._write_last_known_id(VAULT_ID)
        self.assertEqual(
            resolve_active_vault_id(self.config, OTHER_VAULT_ID),
            OTHER_VAULT_ID,
        )

    def test_override_wins_when_disk_has_no_vault_section(self) -> None:
        # Fresh config — no vault block. Override fully bypasses the
        # "no vault opened" placeholder.
        self.assertEqual(
            resolve_active_vault_id(self.config, VAULT_ID),
            VAULT_ID,
        )

    def test_falls_back_to_last_known_id_when_no_override(self) -> None:
        self._write_last_known_id(VAULT_ID)
        self.assertEqual(
            resolve_active_vault_id(self.config, None),
            VAULT_ID,
        )

    def test_empty_string_treated_like_no_override(self) -> None:
        # Defensive: callers should normalize via parse_vault_id_arg
        # first, but if they don't, an empty string should fall back
        # rather than break the resolver.
        self._write_last_known_id(VAULT_ID)
        self.assertEqual(
            resolve_active_vault_id(self.config, ""),
            VAULT_ID,
        )

    def test_returns_empty_when_no_override_and_no_last_known_id(self) -> None:
        self.assertEqual(
            resolve_active_vault_id(self.config, None),
            "",
        )

    def test_reload_picks_up_disk_change(self) -> None:
        # The wizard runs in one subprocess and writes last_known_id;
        # vault-main runs in another and must see the new id without
        # re-spawning. Resolve_active_vault_id calls config.reload()
        # on the no-override path to make that work.
        self.assertEqual(
            resolve_active_vault_id(self.config, None),
            "",
        )
        self._write_last_known_id(VAULT_ID)
        self.assertEqual(
            resolve_active_vault_id(self.config, None),
            VAULT_ID,
        )

    def test_reload_skipped_when_override_present(self) -> None:
        # Override path bypasses reload to avoid the I/O on every
        # local_vault_id() call inside hot loops (browser nav, etc).
        # We can't observe "no I/O" cleanly without mocking, so we
        # assert the value path: override wins regardless of disk state
        # written *after* config construction.
        self._write_last_known_id(VAULT_ID)
        self.assertEqual(
            resolve_active_vault_id(self.config, OTHER_VAULT_ID),
            OTHER_VAULT_ID,
        )


class DispatcherWiringSourceTests(unittest.TestCase):
    """Source pins for the dispatcher — F-U14 anti-regression.

    The dispatcher must:
      1. accept ``--vault-id`` as an optional argparse arg,
      2. validate it via ``parse_vault_id_arg`` and surface bad input
         as a clean ``parser.error()`` call, and
      3. thread the normalized value as a kwarg into every vault
         window that has a vault context (main / browser / import).

    ``vault-onboard`` deliberately does *not* receive the override —
    the wizard creates a new vault, so an override would be
    nonsensical there.
    """

    def setUp(self) -> None:
        self.source = Path(
            REPO_ROOT, "desktop/src/windows.py"
        ).read_text(encoding="utf-8")

    def test_argparse_registers_vault_id(self) -> None:
        self.assertIn('"--vault-id"', self.source)

    def test_dispatcher_imports_parser(self) -> None:
        self.assertIn(
            "from .vault.ui.window_args import parse_vault_id_arg",
            self.source,
        )

    def test_dispatcher_calls_parser_with_argparse_value(self) -> None:
        # The parsed value must be derived from args.vault_id, not
        # left as the raw string. parse_vault_id_arg returns the
        # canonical 12-char form (or None) which is what each window
        # expects to compare against.
        self.assertIn("parse_vault_id_arg(args.vault_id)", self.source)

    def test_dispatcher_surfaces_bad_id_as_argparse_error(self) -> None:
        # Bad input -> parser.error(...) so the subprocess fails fast
        # with a non-zero exit code instead of silently routing to the
        # "no vault opened" placeholder.
        self.assertIn("parser.error(", self.source)
        self.assertIn("--vault-id:", self.source)

    def test_main_window_receives_override(self) -> None:
        self.assertIn(
            "show_vault_main(config_dir, vault_id_override=vault_id_override)",
            self.source,
        )

    def test_browser_window_receives_override(self) -> None:
        self.assertIn(
            "show_vault_browser(config_dir, vault_id_override=vault_id_override)",
            self.source,
        )

    def test_import_window_receives_override(self) -> None:
        self.assertIn(
            "show_vault_import(config_dir, vault_id_override=vault_id_override)",
            self.source,
        )

    def test_onboard_window_does_not_receive_override(self) -> None:
        # Onboard creates a fresh vault — an override would be a
        # contradiction. Pin the call shape so a future refactor
        # doesn't accidentally start passing a stale override here.
        self.assertIn("show_vault_onboard(config_dir)", self.source)
        self.assertNotIn(
            "show_vault_onboard(config_dir, vault_id_override=",
            self.source,
        )

    def test_passphrase_generator_does_not_receive_override(self) -> None:
        # Pure UI — no vault dependency. Same pin shape as onboard.
        self.assertIn(
            "show_vault_passphrase_generator(config_dir)",
            self.source,
        )
        self.assertNotIn(
            "show_vault_passphrase_generator(config_dir, vault_id_override=",
            self.source,
        )


class WindowSignatureSourceTests(unittest.TestCase):
    """Source pins for each window's signature: every vault window
    that *uses* a vault id must accept ``vault_id_override`` and
    thread it through ``resolve_active_vault_id``."""

    def _read(self, rel: str) -> str:
        return Path(REPO_ROOT, rel).read_text(encoding="utf-8")

    def test_show_vault_main_accepts_override(self) -> None:
        source = self._read("desktop/src/windows_vault/main_window.py")
        self.assertIn(
            "def show_vault_main(config_dir: Path, vault_id_override: str | None = None):",
            source,
        )
        self.assertIn(
            "vault_id_undashed = resolve_active_vault_id(config, vault_id_override)",
            source,
        )

    def test_show_vault_browser_accepts_override(self) -> None:
        # The browser is now a package; the entry-point lives in app.py.
        source = self._read("desktop/src/windows_vault_browser/app.py")
        self.assertIn(
            "def show_vault_browser(\n    config_dir: Path,\n    vault_id_override: str | None = None,\n) -> None:",
            source,
        )
        # v2 stores the override on the class and binds the resolver as
        # an instance attribute via lambda, so the body reads
        # ``self.config`` / ``self.vault_id_override`` instead of the
        # closure-captured locals v1 used.
        self.assertIn(
            "resolve_active_vault_id(self.config, self.vault_id_override)",
            source,
        )

    def test_show_vault_import_accepts_override(self) -> None:
        source = self._read("desktop/src/windows_vault_import.py")
        self.assertIn(
            "def show_vault_import(config_dir: Path, vault_id_override: str | None = None) -> None:",
            source,
        )
        self.assertIn(
            "return resolve_active_vault_id(config, vault_id_override)",
            source,
        )


if __name__ == "__main__":
    unittest.main()
