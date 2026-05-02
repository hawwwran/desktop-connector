"""Vault v1 cross-platform test vectors harness.

Discovers JSON case files under ``tests/protocol/vault-v1/`` and exercises each
case against both the desktop Python primitives and (once T2 lands) the server
PHP primitives via a vector-runner CLI.

Schema lock: T0 §A18 in
``docs/plans/desktop-connector-vault-plan-md/desktop-connector-vault-T0-decisions.md``.
Byte-format reference: ``docs/protocol/vault-v1-formats.md``.

In T0.4 the per-primitive files are stubbed to empty arrays so this harness
runs and reports ``0 vectors loaded`` without crashing. T2 fills them in and
adds the actual crypto exercise; until then the harness only validates that
the files exist, parse, and have a shape the case-loader can later consume.
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any

VECTORS_DIR = os.path.join(os.path.dirname(__file__), "vault-v1")

# The set of primitive files T0.4 stubs. Adding entries here is intentional
# during T2 (op_log_segment_v1.json arrives then) — keep this list synced with
# the README in tests/protocol/vault-v1/.
EXPECTED_FILES = (
    "manifest_v1.json",
    "chunk_v1.json",
    "header_v1.json",
    "recovery_envelope_v1.json",
    "device_grant_v1.json",
    "export_bundle_v1.json",
)


def _load_cases(filename: str) -> list[dict[str, Any]]:
    """Return the parsed JSON array from a case file. Raises on malformed JSON."""
    path = os.path.join(VECTORS_DIR, filename)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{filename}: expected JSON array, got {type(data).__name__}")
    return data


def _validate_case_shape(filename: str, index: int, case: dict[str, Any]) -> None:
    """Verify a single case has the T0 §A18 shape. Cheap structural check only."""
    if not isinstance(case, dict):
        raise ValueError(f"{filename}[{index}]: case must be an object")
    for required in ("name", "description", "inputs", "expected"):
        if required not in case:
            raise ValueError(f"{filename}[{index}]: missing required key '{required}'")
    if not isinstance(case["inputs"], dict):
        raise ValueError(f"{filename}[{index}]: 'inputs' must be an object")
    if not isinstance(case["expected"], dict):
        raise ValueError(f"{filename}[{index}]: 'expected' must be an object")


class VaultV1VectorsTests(unittest.TestCase):
    """Discovery + schema-shape harness for T0.4. Crypto exercise comes in T2."""

    def test_vectors_directory_exists(self) -> None:
        self.assertTrue(
            os.path.isdir(VECTORS_DIR),
            f"missing {VECTORS_DIR} — see T0.4 in VAULT-progress.md",
        )

    def test_all_primitive_files_present(self) -> None:
        for name in EXPECTED_FILES:
            with self.subTest(file=name):
                self.assertTrue(
                    os.path.isfile(os.path.join(VECTORS_DIR, name)),
                    f"expected stub file {name} under {VECTORS_DIR}",
                )

    def test_files_parse_as_json_arrays(self) -> None:
        for name in EXPECTED_FILES:
            with self.subTest(file=name):
                cases = _load_cases(name)
                self.assertIsInstance(cases, list)

    def test_case_shape_matches_a18(self) -> None:
        for name in EXPECTED_FILES:
            cases = _load_cases(name)
            for index, case in enumerate(cases):
                with self.subTest(file=name, index=index, case=case.get("name", "?")):
                    _validate_case_shape(name, index, case)

    def test_total_loaded_count_reported(self) -> None:
        total = sum(len(_load_cases(name)) for name in EXPECTED_FILES)
        # Stdout for human visibility when running pytest -s.
        # T0.4 expects 0; T2 fills these in and the count grows.
        print(f"\n[vault-v1 vectors] {total} vectors loaded across {len(EXPECTED_FILES)} files")


if __name__ == "__main__":
    unittest.main()
