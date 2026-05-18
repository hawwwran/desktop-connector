"""T8.1 + T8.2 — Vault export bundle writer / verifier."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _paths import ensure_desktop_on_path  # noqa: E402

ensure_desktop_on_path()

from src.vault import Vault  # noqa: E402
from src.vault.crypto import DefaultVaultCrypto  # noqa: E402
from src.vault.export.bundle import (  # noqa: E402
    ExportError,
    OUTER_HEADER_BYTES,
    WRAPPED_KEY_BYTES,
    RECORD_LEN_BYTES,
    NONCE_BYTES,
    read_export_bundle,
    write_export_bundle,
)
from src.vault.manifest import (  # noqa: E402
    assemble_unified_manifest,
    make_manifest,
    make_remote_folder,
)
from src.vault.upload import upload_file  # noqa: E402

from tests.protocol.test_desktop_vault_manifest import (  # noqa: E402
    AUTHOR,
    DOCS_ID,
    MASTER_KEY,
    VAULT_ID,
)
from tests.protocol.test_desktop_vault_upload import (  # noqa: E402
    FakeUploadRelay,
    seed_sharded_state,
)


VAULT_ACCESS_SECRET = "vault-secret"
PASSPHRASE = "user-export-passphrase"
# The unit tests use the cheapest Argon2id params libsodium accepts so the
# whole suite stays under a few hundred ms; production exports default to
# the §12.2 v1 lock (128 MiB / 4 iterations) inside vault_export.
ARGON_MEMORY_KIB = 8192
ARGON_ITERATIONS = 2


class VaultExportWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_export_test_"))
        self._saved_xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        if self._saved_xdg_cache_home is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg_cache_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_export_writes_atomically_and_records_round_trip(self) -> None:
        """T8.1 acceptance: bundle bytes survive write + read; chunk
        envelopes match the relay's stored copies; no temp file leaks."""
        manifest, relay = self._populated_relay({
            "alpha.txt": b"alpha content here",
            "nested/beta.txt": b"beta content here, distinct bytes",
        })
        bundle_path = self.tmpdir / "vault.dcvault"
        manifest_envelope = _build_legacy_manifest_envelope(manifest)
        vault = self._vault()
        try:
            result = write_export_bundle(
                vault=vault,
                relay=relay,
                manifest_envelope=manifest_envelope,
                manifest_plaintext=manifest,
                output_path=bundle_path,
                passphrase=PASSPHRASE,
                argon_memory_kib=ARGON_MEMORY_KIB,
                argon_iterations=ARGON_ITERATIONS,
            )
        finally:
            vault.close()

        self.assertTrue(bundle_path.exists())
        # No leftover temp files.
        self.assertFalse(list(bundle_path.parent.glob("*.dc-temp-*")))

        # Sanity-check the on-disk size envelope: outer + wrapped + N records.
        self.assertGreaterEqual(
            result.bytes_written,
            OUTER_HEADER_BYTES + WRAPPED_KEY_BYTES + RECORD_LEN_BYTES + NONCE_BYTES,
        )
        self.assertEqual(bundle_path.stat().st_size, result.bytes_written)
        # Header + manifest + chunks + footer.
        self.assertEqual(result.record_count, 1 + 1 + len(relay.chunks) + 1)

        contents = read_export_bundle(
            bundle_path=bundle_path,
            passphrase=PASSPHRASE,
            vault_id=VAULT_ID,
        )
        self.assertEqual(contents.header.vault_id, VAULT_ID)
        self.assertEqual(contents.manifest_envelope, manifest_envelope)
        # Every chunk on the relay round-trips verbatim through the bundle.
        self.assertEqual(set(contents.chunks), set(relay.chunks))
        for cid, envelope in contents.chunks.items():
            self.assertEqual(envelope, relay.chunks[cid])

    def test_export_round_trips_genesis_fingerprint(self) -> None:
        """Review §5.H1: ``write_export_bundle`` accepts an optional
        ``genesis_fingerprint`` kwarg and persists it in the bundle's
        RECORD_TYPE_HEADER plaintext. ``read_export_bundle`` returns
        it on ``BundleHeaderInfo.genesis_fingerprint``. The import
        wizard's §D9 identity gate needs this anchor — without it
        the gate short-circuits on vault_id alone (the §5.H1 bug)."""
        fp = "0123456789abcdef" * 4
        manifest, relay = self._populated_relay({"x.txt": b"y"})
        manifest_envelope = _build_legacy_manifest_envelope(manifest)
        bundle_path = self.tmpdir / "fp.dcvault"
        vault = self._vault()
        try:
            write_export_bundle(
                vault=vault, relay=relay,
                manifest_envelope=manifest_envelope,
                manifest_plaintext=manifest,
                output_path=bundle_path,
                passphrase=PASSPHRASE,
                argon_memory_kib=ARGON_MEMORY_KIB,
                argon_iterations=ARGON_ITERATIONS,
                genesis_fingerprint=fp,
            )
        finally:
            vault.close()
        contents = read_export_bundle(
            bundle_path=bundle_path,
            passphrase=PASSPHRASE,
            vault_id=VAULT_ID,
        )
        self.assertEqual(contents.header.genesis_fingerprint, fp)

    def test_export_without_genesis_fingerprint_reads_as_none(self) -> None:
        """Review §5.H1: omitting the kwarg keeps the bundle byte-
        compatible with older readers — the field is simply absent
        from the JSON header, and the reader's default for
        ``BundleHeaderInfo.genesis_fingerprint`` is ``None``. The
        import path then falls back to vault_id-only matching."""
        manifest, relay = self._populated_relay({"x.txt": b"y"})
        manifest_envelope = _build_legacy_manifest_envelope(manifest)
        bundle_path = self.tmpdir / "no-fp.dcvault"
        vault = self._vault()
        try:
            write_export_bundle(
                vault=vault, relay=relay,
                manifest_envelope=manifest_envelope,
                manifest_plaintext=manifest,
                output_path=bundle_path,
                passphrase=PASSPHRASE,
                argon_memory_kib=ARGON_MEMORY_KIB,
                argon_iterations=ARGON_ITERATIONS,
            )
        finally:
            vault.close()
        contents = read_export_bundle(
            bundle_path=bundle_path,
            passphrase=PASSPHRASE,
            vault_id=VAULT_ID,
        )
        self.assertIsNone(contents.header.genesis_fingerprint)

    def test_export_killed_mid_write_leaves_no_partial_bundle(self) -> None:
        """§A10 acceptance shape: a death mid-write leaves nothing in
        the destination. A retry produces a complete file from the same
        vault state."""
        manifest, relay = self._populated_relay({"a.txt": b"a", "b.txt": b"b" * 32})
        manifest_envelope = _build_legacy_manifest_envelope(manifest)
        bundle_path = self.tmpdir / "vault.dcvault"

        # First attempt: relay raises after the second chunk fetch.
        crashed_after = [0]

        class FlakyRelay:
            def __init__(self, real, fail_after):
                self.real = real
                self.fail_after = fail_after
                self.calls = 0

            def get_chunk(self, vault_id, vault_access_secret, chunk_id):
                self.calls += 1
                crashed_after[0] = self.calls
                if self.calls > self.fail_after:
                    raise RuntimeError("simulated kill")
                return self.real.get_chunk(vault_id, vault_access_secret, chunk_id)

        flaky = FlakyRelay(relay, fail_after=1)
        vault = self._vault()
        try:
            with self.assertRaises(RuntimeError):
                write_export_bundle(
                    vault=vault, relay=flaky,
                    manifest_envelope=manifest_envelope,
                    manifest_plaintext=manifest,
                    output_path=bundle_path,
                    passphrase=PASSPHRASE,
                    argon_memory_kib=ARGON_MEMORY_KIB,
                    argon_iterations=ARGON_ITERATIONS,
                )

            self.assertFalse(bundle_path.exists())
            self.assertFalse(list(bundle_path.parent.glob("*.dc-temp-*")))

            # Retry on the real relay produces a full bundle that decrypts.
            result = write_export_bundle(
                vault=vault, relay=relay,
                manifest_envelope=manifest_envelope,
                manifest_plaintext=manifest,
                output_path=bundle_path,
                passphrase=PASSPHRASE,
                argon_memory_kib=ARGON_MEMORY_KIB,
                argon_iterations=ARGON_ITERATIONS,
            )
        finally:
            vault.close()

        self.assertTrue(bundle_path.exists())
        contents = read_export_bundle(
            bundle_path=bundle_path,
            passphrase=PASSPHRASE,
            vault_id=VAULT_ID,
        )
        self.assertEqual(set(contents.chunks), set(relay.chunks))
        self.assertGreater(crashed_after[0], 0)


class VaultExportVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vault_export_verify_test_"))
        self._saved_xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.tmpdir / "xdg_cache")

    def tearDown(self) -> None:
        if self._saved_xdg_cache_home is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved_xdg_cache_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_one_byte_tamper_in_middle_is_detected(self) -> None:
        """T8.2 acceptance: flipping any byte after the outer header
        breaks either the per-record AEAD or the footer's hash chain."""
        bundle_path = self._build_bundle(b"some content under tamper test")
        raw = bundle_path.read_bytes()

        # Pick a byte well past the outer header to ensure we're inside
        # the encrypted record stream.
        tamper_offset = OUTER_HEADER_BYTES + WRAPPED_KEY_BYTES + 50
        self.assertLess(tamper_offset, len(raw) - 1)
        tampered = bytearray(raw)
        tampered[tamper_offset] ^= 0x01
        bundle_path.write_bytes(bytes(tampered))

        with self.assertRaises(ExportError) as ctx:
            read_export_bundle(
                bundle_path=bundle_path,
                passphrase=PASSPHRASE,
                vault_id=VAULT_ID,
            )
        self.assertEqual(ctx.exception.code, "vault_export_tampered")

    def test_wrong_passphrase_fails_with_typed_error(self) -> None:
        bundle_path = self._build_bundle(b"correct passphrase only")
        with self.assertRaises(ExportError) as ctx:
            read_export_bundle(
                bundle_path=bundle_path,
                passphrase="WRONG",
                vault_id=VAULT_ID,
            )
        self.assertEqual(ctx.exception.code, "vault_export_passphrase_invalid")

    def test_truncated_bundle_fails_cleanly(self) -> None:
        bundle_path = self._build_bundle(b"complete to start")
        raw = bundle_path.read_bytes()
        # Drop the last 200 bytes — well inside the chunk/footer area.
        bundle_path.write_bytes(raw[: max(OUTER_HEADER_BYTES + WRAPPED_KEY_BYTES, len(raw) - 200)])
        with self.assertRaises(ExportError) as ctx:
            read_export_bundle(
                bundle_path=bundle_path,
                passphrase=PASSPHRASE,
                vault_id=VAULT_ID,
            )
        self.assertIn(ctx.exception.code, ("vault_export_truncated", "vault_export_tampered"))

    def test_export_refuses_short_passphrase(self) -> None:
        """Review §5.M5 — bottom-of-the-stack passphrase floor. The
        wizard restricts the picker UI; this is the safety net against
        a power-user supplying ``"x"`` via the library API. Pre-fix
        no length gate existed and Argon2id derivation would happily
        produce a wrap key for a 1-character secret — well within
        offline brute-force reach.
        """
        from src.vault.export.bundle import EXPORT_PASSPHRASE_MIN_LEN
        bundle_path = self.tmpdir / "vault.dcvault"
        manifest, relay = _populated_relay_with(self.tmpdir, {"x.txt": b"y"})
        vault = _vault()
        try:
            with self.assertRaises(ExportError) as ctx:
                write_export_bundle(
                    vault=vault, relay=relay,
                    manifest_envelope=_build_legacy_manifest_envelope(manifest),
                    manifest_plaintext=manifest,
                    output_path=bundle_path,
                    passphrase="x",  # well below the floor
                    argon_memory_kib=ARGON_MEMORY_KIB,
                    argon_iterations=ARGON_ITERATIONS,
                )
            self.assertEqual(
                ctx.exception.code, "vault_export_passphrase_too_short",
            )
            self.assertIn(str(EXPORT_PASSPHRASE_MIN_LEN), str(ctx.exception))
        finally:
            vault.close()
        # No bundle created.
        self.assertFalse(bundle_path.exists())

    def test_bad_outer_magic_rejected_immediately(self) -> None:
        bundle_path = self._build_bundle(b"correct magic only")
        raw = bytearray(bundle_path.read_bytes())
        raw[0:4] = b"XXXX"
        bundle_path.write_bytes(bytes(raw))
        with self.assertRaises(ExportError) as ctx:
            read_export_bundle(
                bundle_path=bundle_path,
                passphrase=PASSPHRASE,
                vault_id=VAULT_ID,
            )
        self.assertEqual(ctx.exception.code, "vault_export_unknown_format")

    def test_chunk_record_reorder_fails_closed(self) -> None:
        """Review §7.H4: swapping two chunk records' on-disk positions
        must break decryption. Spec §16: each record's ``record_index``
        is bound into its AAD via ``build_export_record_aad``, so a
        record placed at the wrong index AEAD-fails its tag check;
        the footer's hash chain (which is a sequential digest over
        all records in order) also drifts. Either failure surfaces as
        ``vault_export_tampered``.

        Pre-fix the existing tamper tests covered single-byte
        flips + truncation, which already triggered the AEAD or
        hash-chain guards. Reorder is a logically distinct attack
        vector — an adversary swaps two encrypted chunk blobs in the
        hope that one chunk's plaintext gets misattributed to the
        other path. Pin it here so a future refactor that removes
        ``record_index`` from the AAD silently widens the threat
        surface."""
        manifest, relay = _populated_relay_with(self.tmpdir, {
            "a.txt": b"AAAAAAAAAAAAAAAA-record-zero",
            "b.txt": b"BBBBBBBBBBBBBBBB-record-one-distinct-bytes",
        })
        bundle_path = self.tmpdir / "vault.dcvault"
        manifest_envelope = _build_legacy_manifest_envelope(manifest)
        vault = _vault()
        try:
            write_export_bundle(
                vault=vault, relay=relay,
                manifest_envelope=manifest_envelope,
                manifest_plaintext=manifest,
                output_path=bundle_path,
                passphrase=PASSPHRASE,
                argon_memory_kib=ARGON_MEMORY_KIB,
                argon_iterations=ARGON_ITERATIONS,
            )
        finally:
            vault.close()

        raw = bytearray(bundle_path.read_bytes())
        cursor = OUTER_HEADER_BYTES + WRAPPED_KEY_BYTES
        # Walk records: header (idx 0), manifest (idx 1), chunk (idx 2),
        # chunk (idx 3), footer. Identify the two chunk records by
        # ordinal — we need indices 2 and 3.
        record_offsets: list[tuple[int, int]] = []
        idx = 0
        while cursor < len(raw):
            length = int.from_bytes(
                raw[cursor:cursor + RECORD_LEN_BYTES], "big",
            )
            start = cursor
            end = cursor + RECORD_LEN_BYTES + length
            record_offsets.append((start, end))
            cursor = end
            idx += 1
        self.assertGreaterEqual(
            len(record_offsets), 5,
            "two chunks must produce at least 5 on-disk records",
        )
        chunk_a_start, chunk_a_end = record_offsets[2]
        chunk_b_start, chunk_b_end = record_offsets[3]
        chunk_a_bytes = bytes(raw[chunk_a_start:chunk_a_end])
        chunk_b_bytes = bytes(raw[chunk_b_start:chunk_b_end])
        # Reassemble bundle with chunk records swapped.
        tampered = (
            bytes(raw[:chunk_a_start])
            + chunk_b_bytes
            + chunk_a_bytes
            + bytes(raw[chunk_b_end:])
        )
        # Sanity: total length preserved (records are the same on-disk
        # size structure; just two chunks in different slots).
        self.assertEqual(len(tampered), len(raw))
        bundle_path.write_bytes(tampered)

        with self.assertRaises(ExportError) as ctx:
            read_export_bundle(
                bundle_path=bundle_path,
                passphrase=PASSPHRASE,
                vault_id=VAULT_ID,
            )
        self.assertEqual(ctx.exception.code, "vault_export_tampered")

    def _build_bundle(self, content: bytes) -> Path:
        manifest, relay = _populated_relay_with(self.tmpdir, {"file.bin": content})
        bundle_path = self.tmpdir / "vault.dcvault"
        vault = _vault()
        try:
            write_export_bundle(
                vault=vault, relay=relay,
                manifest_envelope=_build_legacy_manifest_envelope(manifest),
                manifest_plaintext=manifest,
                output_path=bundle_path,
                passphrase=PASSPHRASE,
                argon_memory_kib=ARGON_MEMORY_KIB,
                argon_iterations=ARGON_ITERATIONS,
            )
        finally:
            vault.close()
        return bundle_path


# ---------------------------------------------------------------------------
# Helpers shared between writer + verifier tests.
# ---------------------------------------------------------------------------


def _vault() -> Vault:
    return Vault(
        vault_id=VAULT_ID,
        master_key=MASTER_KEY,
        recovery_secret=None,
        vault_access_secret=VAULT_ACCESS_SECRET,
        header_revision=1,
        manifest_revision=1,
        manifest_ciphertext=b"",
        crypto=DefaultVaultCrypto,
    )


def _empty_manifest() -> dict:
    return make_manifest(
        vault_id=VAULT_ID,
        revision=1,
        parent_revision=0,
        created_at="2026-05-04T12:00:00.000Z",
        author_device_id=AUTHOR,
        remote_folders=[
            make_remote_folder(
                remote_folder_id=DOCS_ID,
                display_name_enc="Documents",
                created_at="2026-05-04T12:00:00.000Z",
                created_by_device_id=AUTHOR,
                entries=[],
            )
        ],
    )


def _populated_relay_with(tmpdir: Path, files: dict[str, bytes]) -> tuple[dict, FakeUploadRelay]:
    manifest = _empty_manifest()
    relay = FakeUploadRelay()
    vault = _vault()
    try:
        seed_sharded_state(
                vault, relay,
                vault_id=manifest['vault_id'],
                remote_folders=manifest['remote_folders'],
                created_at=manifest['created_at'],
                author_device_id=manifest['author_device_id'],
            )
        current_manifest = manifest
        for relative, content in files.items():
            local = tmpdir / relative.replace("/", "_")
            local.write_bytes(content)
            res = upload_file(
                vault=vault, relay=relay, manifest=current_manifest,
                local_path=local, remote_folder_id=DOCS_ID,
                remote_path=relative, author_device_id=AUTHOR,
            )
            current_manifest = assemble_unified_manifest(res.root, {res.remote_folder_id: res.shard})
    finally:
        vault.close()
    # Assemble the unified manifest view that the export bundle still
    # embeds as a single ``manifest_envelope`` record. After Phase H
    # the relay no longer maintains a legacy single envelope, so we
    # build it here from the post-upload sharded state.
    vault_observer = _vault()
    try:
        root = vault_observer.fetch_root_manifest(relay)
        shards = {
            pointer["remote_folder_id"]: vault_observer.fetch_folder_shard(
                relay, pointer["remote_folder_id"],
            )
            for pointer in root.get("remote_folders", [])
            if pointer.get("shard_hash")
        }
        published = assemble_unified_manifest(root, shards)
    finally:
        vault_observer.close()
    return published, relay


def _build_legacy_manifest_envelope(manifest: dict) -> bytes:
    """Encrypt a unified manifest dict into the legacy single-envelope
    bytes shape that the export bundle still embeds. Phase H removed
    the relay-side surface, but the bundle format keeps a single
    ``RECORD_TYPE_MANIFEST`` record for backwards compatibility on
    older readers — synthesize it here from the unified plaintext.
    """
    from src.vault.crypto import (
        aead_encrypt,
        build_manifest_aad,
        build_manifest_envelope,
        derive_subkey,
    )
    from src.vault.manifest import canonical_manifest_json, normalize_manifest_plaintext
    import secrets as _secrets

    normalized = normalize_manifest_plaintext(manifest)
    plaintext = canonical_manifest_json(normalized)
    subkey = derive_subkey("dc-vault-v1/manifest", bytes(MASTER_KEY))
    nonce = _secrets.token_bytes(24)
    aad = build_manifest_aad(
        vault_id=str(normalized["vault_id"]),
        revision=int(normalized["revision"]),
        parent_revision=int(normalized["parent_revision"]),
        author_device_id=str(normalized["author_device_id"]),
    )
    ciphertext = aead_encrypt(plaintext, subkey, nonce, aad)
    return build_manifest_envelope(
        vault_id=str(normalized["vault_id"]),
        revision=int(normalized["revision"]),
        parent_revision=int(normalized["parent_revision"]),
        author_device_id=str(normalized["author_device_id"]),
        nonce=nonce,
        aead_ciphertext_and_tag=ciphertext,
    )


# Convenience method on the test class so each test reads naturally.
VaultExportWriterTests._populated_relay = lambda self, files: _populated_relay_with(self.tmpdir, files)
VaultExportWriterTests._vault = lambda self: _vault()


if __name__ == "__main__":
    unittest.main()
