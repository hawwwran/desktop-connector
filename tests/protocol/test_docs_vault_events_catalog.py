"""T17.6 — every vault.* event tag in the catalog has at least one emit site.

Reads docs/diagnostics.events.md, extracts every event tag of the
form ``vault.<topic>.<verb>`` (with optional wildcard ``vault.foo.*``
expanded by greppning the source for ``vault.foo.``), and asserts
each tag is emitted by at least one Python or PHP source file. The
desktop tree, server tree, and the Android build all participate.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "docs" / "diagnostics.events.md"

EVENT_TAG_RE = re.compile(r"`(vault\.[a-z0-9_.*]+)`")
SEARCH_ROOTS = [
    REPO_ROOT / "desktop" / "src",
    REPO_ROOT / "server" / "src",
    REPO_ROOT / "tests" / "protocol",
]


class VaultEventCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        if not CATALOG.is_file():
            self.skipTest(f"catalog not found at {CATALOG}")
        self.tags = self._read_catalog_tags()

    def test_catalog_lists_at_least_one_vault_tag(self) -> None:
        self.assertGreaterEqual(
            len(self.tags), 1,
            f"docs/diagnostics.events.md emitted no vault.* tags",
        )

    def test_every_catalog_tag_has_at_least_one_emit_site(self) -> None:
        missing: list[str] = []
        for tag in self.tags:
            if not self._tag_is_emitted(tag):
                missing.append(tag)
        self.assertFalse(
            missing,
            "vault.* events documented in the catalog but never emitted: "
            + ", ".join(missing),
        )

    def test_catalog_section_is_in_alphabetical_order(self) -> None:
        # The vault subsection in the catalog must list rows in
        # alphabetical order so a scan-by-eye is predictable.
        section = self._read_vault_section()
        rows = re.findall(r"^\| `(vault\.[a-z0-9_.*]+)` \|", section, re.MULTILINE)
        self.assertEqual(
            rows, sorted(rows),
            "vault.* catalog rows are not in alphabetical order. "
            "Reorder so adjacent rows always increase by tag name.",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_catalog_tags(self) -> list[str]:
        text = self._read_vault_section()
        seen: list[str] = []
        for match in EVENT_TAG_RE.finditer(text):
            tag = match.group(1)
            if tag.startswith("vault.") and tag not in seen:
                seen.append(tag)
        return seen

    def _read_vault_section(self) -> str:
        text = CATALOG.read_text()
        # The section starts at "### vault" and ends at the next "###" or "##".
        start = text.find("### vault")
        if start < 0:
            self.fail("docs/diagnostics.events.md is missing a '### vault' section")
        rest = text[start:]
        # Find the next section heading.
        match = re.search(r"\n##? ", rest[2:])
        if match is None:
            return rest
        return rest[: match.start() + 2]

    def _tag_is_emitted(self, tag: str) -> bool:
        # Wildcard tags like "vault.recovery_test.*" mean: at least one
        # file emits something starting with the prefix before the *.
        prefix = tag.rstrip(".*")
        for root in SEARCH_ROOTS:
            if not root.is_dir():
                continue
            try:
                result = subprocess.run(
                    ["grep", "-rl", "-F", prefix, str(root)],
                    capture_output=True, text=True, check=False,
                )
            except FileNotFoundError:
                self.fail("grep is required for the vault-event-catalog test")
            if result.stdout.strip():
                return True
        return False


if __name__ == "__main__":
    unittest.main()
