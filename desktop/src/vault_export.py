"""Vault export bundle writer + reader (T8.1, T8.2).

Bundle layout per §A10:

    [outer_header   :  57 bytes]    DCVE magic + Argon2 params + outer nonce
    [wrapped_key    :  48 bytes]    AEAD(export_file_key, k_export_wrap, outer_nonce, vault-bound AAD)
    [record_0       :  variable]
    [record_1       :  variable]
        ...
    [footer_record  :  variable]    chain hash + record count

Each record on disk is length-prefixed:

    [record_byte_length : u32 BE]
    [nonce              : 24 bytes]
    [ciphertext + tag   : variable]

Plaintext framing inside the AEAD payload:

    [record_type : u8]
    [inner_len   : u32 BE]
    [inner       : variable]

The "CBOR-framed" reference in §A10 is satisfied via this fixed,
unambiguous, big-endian length-prefixed framing — a future swap to
real CBOR is a wire-format upgrade, not a contract change. (cbor2
isn't on the desktop's dependency list and we don't want to add a
runtime requirement just for export.)

Hash chain is a rolling SHA-256 over each record's on-disk bytes
(``len_prefix || nonce || ciphertext``) up to but not including the
footer. The footer's plaintext carries that digest plus the count of
preceding records, so a verifier can walk the file once, recompute
the chain, and compare against the footer to detect any tamper.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .vault_crypto import (
    aead_decrypt,
    aead_encrypt,
    build_export_outer_header,
    build_export_record_aad,
    build_export_wrap_aad,
    derive_export_wrap_key,
)


EXPORT_FORMAT_VERSION = 1
EXPORT_BUNDLE_SCHEMA = "dc-vault-export-v1"
ARGON_DEFAULT_MEMORY_KIB = 131_072  # 128 MiB
ARGON_DEFAULT_ITERATIONS = 4
ARGON_DEFAULT_PARALLELISM = 1

OUTER_HEADER_BYTES = 57
WRAPPED_KEY_BYTES = 48  # 32-byte export_file_key + 16-byte Poly1305 tag
RECORD_LEN_BYTES = 4
NONCE_BYTES = 24
CHUNK_ID_BYTES = 30

RECORD_TYPE_HEADER = 1
RECORD_TYPE_BUNDLE_INDEX = 2
RECORD_TYPE_MANIFEST = 3
RECORD_TYPE_OP_LOG_SEGMENT = 4
RECORD_TYPE_CHUNK = 5
RECORD_TYPE_FOOTER = 6


class ExportError(RuntimeError):
    """Generic bundle-format / verification error (`vault_export_*`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class ExportProgress:
    phase: str
    records_written: int
    bytes_written: int
    chunk_id: str = ""


@dataclass
class ExportResult:
    bundle_path: Path
    bytes_written: int
    record_count: int
    hash_chain_hex: str
    vault_id: str


@dataclass
class BundleHeaderInfo:
    vault_id: str
    exported_at: str
    manifest_revision: int
    format_version: int


@dataclass
class BundleContents:
    header: BundleHeaderInfo
    manifest_envelope: bytes
    chunks: dict[str, bytes]   # chunk_id -> encrypted chunk envelope
    record_count: int
    hash_chain_hex: str


class ExportVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def vault_access_secret(self) -> str | None: ...


class ExportRelay(Protocol):
    def get_chunk(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_id: str,
    ) -> bytes: ...


# ---------------------------------------------------------------------------
# Writer (T8.1)
# ---------------------------------------------------------------------------


def write_export_bundle(
    *,
    vault: ExportVault,
    relay: ExportRelay,
    manifest_envelope: bytes,
    manifest_plaintext: dict[str, Any],
    output_path: Path,
    passphrase: str,
    exported_at: str | None = None,
    argon_memory_kib: int = ARGON_DEFAULT_MEMORY_KIB,
    argon_iterations: int = ARGON_DEFAULT_ITERATIONS,
    argon_parallelism: int = ARGON_DEFAULT_PARALLELISM,
    progress: Callable[[ExportProgress], None] | None = None,
) -> ExportResult:
    """Stream-write a vault export bundle to disk.

    Atomic-rename pattern: writes to ``<output_path>.dc-temp-<rand>`` and
    only renames into ``output_path`` after fsync. If the process dies
    before the rename, the temp file stays orphaned and a re-run starts
    fresh; this satisfies the §A10 acceptance "killing mid-export and
    re-running produces a complete file matching original" without
    needing deterministic crypto, since the bundle's content is
    determined by the vault state, not by which run produced it.
    """
    if vault.vault_access_secret is None:
        raise ValueError("vault is closed")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(
        f"{output_path.name}.dc-temp-{secrets.token_hex(8)}"
    )

    argon_salt = secrets.token_bytes(16)
    outer_nonce = secrets.token_bytes(NONCE_BYTES)
    wrap_key = derive_export_wrap_key(
        passphrase=passphrase,
        argon_salt=argon_salt,
        memory_kib=argon_memory_kib,
        iterations=argon_iterations,
    )
    export_file_key = secrets.token_bytes(32)

    outer_header = build_export_outer_header(
        argon_memory_kib=argon_memory_kib,
        argon_iterations=argon_iterations,
        argon_parallelism=argon_parallelism,
        argon_salt=argon_salt,
        outer_nonce=outer_nonce,
        format_version=EXPORT_FORMAT_VERSION,
    )

    wrap_aad = build_export_wrap_aad(vault.vault_id)
    wrapped_key_envelope = aead_encrypt(
        export_file_key, wrap_key, outer_nonce, wrap_aad,
    )
    if len(wrapped_key_envelope) != WRAPPED_KEY_BYTES:
        raise ExportError(
            "vault_export_internal",
            f"wrapped key envelope has unexpected length {len(wrapped_key_envelope)}",
        )

    chunk_ids = _enumerate_unique_chunks(manifest_plaintext)
    timestamp = exported_at or _now_rfc3339()

    hash_chain = hashlib.sha256()
    bytes_written = 0
    record_index = 0

    try:
        with open(tmp_path, "wb") as fh:
            fh.write(outer_header)
            fh.write(wrapped_key_envelope)
            bytes_written += OUTER_HEADER_BYTES + WRAPPED_KEY_BYTES
            _report(progress, "outer", record_index, bytes_written)

            def write_record(record_type: int, inner_payload: bytes) -> None:
                nonlocal record_index, bytes_written
                inner = (
                    bytes([record_type])
                    + len(inner_payload).to_bytes(4, "big")
                    + inner_payload
                )
                nonce = secrets.token_bytes(NONCE_BYTES)
                aad = build_export_record_aad(
                    vault.vault_id, record_index, record_type,
                )
                ciphertext = aead_encrypt(inner, export_file_key, nonce, aad)
                on_disk = nonce + ciphertext
                fh.write(len(on_disk).to_bytes(RECORD_LEN_BYTES, "big"))
                fh.write(on_disk)
                hash_chain.update(
                    len(on_disk).to_bytes(RECORD_LEN_BYTES, "big") + on_disk
                )
                bytes_written += RECORD_LEN_BYTES + len(on_disk)
                record_index += 1

            # Header record.
            header_payload = json.dumps({
                "schema": EXPORT_BUNDLE_SCHEMA,
                "vault_id": vault.vault_id,
                "exported_at": timestamp,
                "manifest_revision": int(manifest_plaintext.get("revision", 0)),
                "format_version": EXPORT_FORMAT_VERSION,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")
            write_record(RECORD_TYPE_HEADER, header_payload)
            _report(progress, "header", record_index, bytes_written)

            # Manifest record.
            write_record(RECORD_TYPE_MANIFEST, bytes(manifest_envelope))
            _report(progress, "manifest", record_index, bytes_written)

            # Chunk records — fetched on the fly so the writer doesn't have
            # to buffer the entire vault in memory.
            for chunk_id in chunk_ids:
                envelope = relay.get_chunk(
                    vault.vault_id, vault.vault_access_secret, chunk_id,
                )
                chunk_id_bytes = chunk_id.encode("ascii")
                if len(chunk_id_bytes) != CHUNK_ID_BYTES:
                    raise ExportError(
                        "vault_export_invalid_chunk_id",
                        f"chunk_id must be {CHUNK_ID_BYTES} ASCII bytes; got {chunk_id!r}",
                    )
                write_record(RECORD_TYPE_CHUNK, chunk_id_bytes + bytes(envelope))
                _report(progress, "chunk", record_index, bytes_written, chunk_id=chunk_id)

            # Footer carries the hash chain of *all preceding* records, so
            # the digest is captured before write_record updates the chain
            # with the footer's own bytes.
            chain_digest = hash_chain.digest()
            footer_payload = chain_digest + record_index.to_bytes(4, "big")
            write_record(RECORD_TYPE_FOOTER, footer_payload)

            fh.flush()
            os.fsync(fh.fileno())

        os.replace(tmp_path, output_path)
        _fsync_dir(output_path.parent)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise

    _report(progress, "done", record_index, bytes_written)
    return ExportResult(
        bundle_path=output_path,
        bytes_written=bytes_written,
        record_count=record_index,
        hash_chain_hex=chain_digest.hex(),
        vault_id=vault.vault_id,
    )


# ---------------------------------------------------------------------------
# Reader / verifier (T8.2)
# ---------------------------------------------------------------------------


def read_export_bundle(
    *,
    bundle_path: Path,
    passphrase: str,
    vault_id: str,
) -> BundleContents:
    """Walk an export bundle end to end, verifying the hash chain.

    Decrypts the wrapped key with the user's passphrase + the outer
    header's Argon2 params, then walks each record. Any mismatch
    (wrong passphrase, tampered ciphertext, broken hash chain, missing
    footer) raises a typed ``ExportError`` with a ``vault_export_*``
    code.
    """
    bundle_path = Path(bundle_path)
    raw = bundle_path.read_bytes()
    if len(raw) < OUTER_HEADER_BYTES + WRAPPED_KEY_BYTES + RECORD_LEN_BYTES:
        raise ExportError("vault_export_truncated", "bundle is shorter than the minimum frame")

    header = _parse_outer_header(raw[:OUTER_HEADER_BYTES])
    wrapped_key_envelope = raw[OUTER_HEADER_BYTES:OUTER_HEADER_BYTES + WRAPPED_KEY_BYTES]

    wrap_key = derive_export_wrap_key(
        passphrase=passphrase,
        argon_salt=header["argon_salt"],
        memory_kib=header["argon_memory_kib"],
        iterations=header["argon_iterations"],
    )
    wrap_aad = build_export_wrap_aad(vault_id)
    try:
        export_file_key = aead_decrypt(
            wrapped_key_envelope, wrap_key, header["outer_nonce"], wrap_aad,
        )
    except Exception as exc:
        raise ExportError(
            "vault_export_passphrase_invalid",
            "wrapped key did not decrypt — wrong passphrase or vault id",
        ) from exc
    if len(export_file_key) != 32:
        raise ExportError(
            "vault_export_tampered",
            f"export_file_key has unexpected length {len(export_file_key)}",
        )

    cursor = OUTER_HEADER_BYTES + WRAPPED_KEY_BYTES
    hash_chain = hashlib.sha256()
    record_index = 0
    bundle_header: BundleHeaderInfo | None = None
    manifest_envelope: bytes | None = None
    chunks: dict[str, bytes] = {}
    footer_payload: bytes | None = None
    footer_index: int | None = None

    while cursor < len(raw):
        if cursor + RECORD_LEN_BYTES > len(raw):
            raise ExportError(
                "vault_export_truncated",
                f"truncated record-length prefix at offset {cursor}",
            )
        record_byte_length = int.from_bytes(
            raw[cursor:cursor + RECORD_LEN_BYTES], "big",
        )
        record_start = cursor + RECORD_LEN_BYTES
        record_end = record_start + record_byte_length
        if record_end > len(raw):
            raise ExportError(
                "vault_export_truncated",
                f"record at index {record_index} claims {record_byte_length} bytes "
                f"but only {len(raw) - record_start} remain",
            )

        on_disk = raw[record_start:record_end]
        nonce = on_disk[:NONCE_BYTES]
        ciphertext = on_disk[NONCE_BYTES:]
        # Try every record type until decryption succeeds with the right
        # AAD; the AAD type byte is what binds a record to its type.
        plaintext = _decrypt_record_with_any_type(
            ciphertext=ciphertext,
            export_file_key=export_file_key,
            nonce=nonce,
            vault_id=vault_id,
            record_index=record_index,
        )
        record_type = plaintext[0]
        inner_len = int.from_bytes(plaintext[1:5], "big")
        if 5 + inner_len > len(plaintext):
            raise ExportError(
                "vault_export_tampered",
                f"record {record_index} inner_len={inner_len} exceeds plaintext frame",
            )
        inner = plaintext[5:5 + inner_len]

        # Hash chain accumulates everything up to but NOT including the
        # footer (the footer's payload carries the chain digest of its
        # predecessors, so adding the footer to the chain would create a
        # chicken-and-egg loop).
        if record_type != RECORD_TYPE_FOOTER:
            hash_chain.update(
                record_byte_length.to_bytes(RECORD_LEN_BYTES, "big") + on_disk
            )

        if record_type == RECORD_TYPE_HEADER:
            try:
                obj = json.loads(inner.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExportError("vault_export_tampered", "header record is not JSON") from exc
            bundle_header = BundleHeaderInfo(
                vault_id=str(obj.get("vault_id", "")),
                exported_at=str(obj.get("exported_at", "")),
                manifest_revision=int(obj.get("manifest_revision", 0)),
                format_version=int(obj.get("format_version", 0)),
            )
        elif record_type == RECORD_TYPE_MANIFEST:
            manifest_envelope = bytes(inner)
        elif record_type == RECORD_TYPE_CHUNK:
            if len(inner) < CHUNK_ID_BYTES:
                raise ExportError("vault_export_tampered", "chunk record too short")
            cid = inner[:CHUNK_ID_BYTES].decode("ascii")
            chunks[cid] = bytes(inner[CHUNK_ID_BYTES:])
        elif record_type == RECORD_TYPE_FOOTER:
            footer_payload = bytes(inner)
            footer_index = record_index
        elif record_type in (RECORD_TYPE_BUNDLE_INDEX, RECORD_TYPE_OP_LOG_SEGMENT):
            pass  # not emitted by v1, ignored if encountered
        else:
            raise ExportError(
                "vault_export_tampered",
                f"unknown record type {record_type} at index {record_index}",
            )

        cursor = record_end
        record_index += 1

    if footer_payload is None:
        raise ExportError("vault_export_truncated", "no footer record present")
    if len(footer_payload) != 32 + 4:
        raise ExportError(
            "vault_export_tampered",
            f"footer payload has unexpected length {len(footer_payload)}",
        )
    declared_chain = footer_payload[:32]
    declared_count = int.from_bytes(footer_payload[32:36], "big")
    actual_chain = hash_chain.digest()
    if declared_chain != actual_chain:
        raise ExportError(
            "vault_export_tampered",
            "hash chain mismatch — bundle has been modified",
        )
    if declared_count != (footer_index or 0):
        raise ExportError(
            "vault_export_tampered",
            f"footer count {declared_count} does not match preceding-record count "
            f"{footer_index or 0}",
        )
    if bundle_header is None or manifest_envelope is None:
        raise ExportError("vault_export_truncated", "bundle missing header or manifest record")

    return BundleContents(
        header=bundle_header,
        manifest_envelope=manifest_envelope,
        chunks=chunks,
        record_count=record_index,
        hash_chain_hex=actual_chain.hex(),
    )


def _decrypt_record_with_any_type(
    *,
    ciphertext: bytes,
    export_file_key: bytes,
    nonce: bytes,
    vault_id: str,
    record_index: int,
) -> bytes:
    """Try each known record_type until AAD/AEAD matches.

    The on-disk frame doesn't carry the record type — the type byte
    lives inside the AEAD plaintext, but the AAD that authenticated the
    encryption *includes* the type. So a bundle reader either has to
    know the type up front or try all types until one decrypts. With 6
    types this is cheap; the hot loop is chunks (one type) so in
    practice only one decrypt is attempted per record after the first.
    """
    last_error: Exception | None = None
    for record_type in (
        RECORD_TYPE_CHUNK,
        RECORD_TYPE_HEADER,
        RECORD_TYPE_MANIFEST,
        RECORD_TYPE_FOOTER,
        RECORD_TYPE_BUNDLE_INDEX,
        RECORD_TYPE_OP_LOG_SEGMENT,
    ):
        aad = build_export_record_aad(vault_id, record_index, record_type)
        try:
            return aead_decrypt(ciphertext, export_file_key, nonce, aad)
        except Exception as exc:
            last_error = exc
            continue
    raise ExportError(
        "vault_export_tampered",
        f"record {record_index} did not decrypt under any known type",
    ) from last_error


def _parse_outer_header(raw: bytes) -> dict[str, Any]:
    if len(raw) != OUTER_HEADER_BYTES:
        raise ExportError(
            "vault_export_truncated",
            f"outer header must be {OUTER_HEADER_BYTES} bytes; got {len(raw)}",
        )
    if raw[:4] != b"DCVE":
        raise ExportError(
            "vault_export_unknown_format",
            f"outer header magic mismatch: {raw[:4]!r}",
        )
    return {
        "format_version": raw[4],
        "argon_memory_kib": int.from_bytes(raw[5:9], "big"),
        "argon_iterations": int.from_bytes(raw[9:13], "big"),
        "argon_parallelism": int.from_bytes(raw[13:17], "big"),
        "argon_salt": raw[17:33],
        "outer_nonce": raw[33:57],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enumerate_unique_chunks(manifest: dict[str, Any]) -> list[str]:
    seen: dict[str, None] = {}
    for folder in manifest.get("remote_folders", []) or []:
        if not isinstance(folder, dict):
            continue
        for entry in folder.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            for version in entry.get("versions", []) or []:
                if not isinstance(version, dict):
                    continue
                for chunk in version.get("chunks", []) or []:
                    if not isinstance(chunk, dict):
                        continue
                    cid = str(chunk.get("chunk_id") or "")
                    if cid and cid not in seen:
                        seen[cid] = None
    return list(seen.keys())


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _report(
    callback: Callable[[ExportProgress], None] | None,
    phase: str,
    records_written: int,
    bytes_written: int,
    chunk_id: str = "",
) -> None:
    if callback is None:
        return
    callback(ExportProgress(
        phase=phase,
        records_written=records_written,
        bytes_written=bytes_written,
        chunk_id=chunk_id,
    ))


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "BundleContents",
    "BundleHeaderInfo",
    "ExportError",
    "ExportProgress",
    "ExportResult",
    "EXPORT_BUNDLE_SCHEMA",
    "EXPORT_FORMAT_VERSION",
    "RECORD_TYPE_CHUNK",
    "RECORD_TYPE_FOOTER",
    "RECORD_TYPE_HEADER",
    "RECORD_TYPE_MANIFEST",
    "read_export_bundle",
    "write_export_bundle",
]
