"""Recovery-kit file: write / parse / verify / shred.

The kit is a plaintext UTF-8 file (LF, mode 0o600) at
``<config_dir>/<vault-id-with-dashes>.dc-vault-recovery``. Security
comes from physical custody (USB / password-manager attachment / safe)
+ the user's passphrase (Argon2id-protected). The relay never sees a
kit file; an attacker who steals only the kit must brute-force the
passphrase against m=128 MiB / t=4 to derive the master key.
"""

import base64
import os
from pathlib import Path

from .crypto import (
    aead_decrypt,
    build_recovery_aad,
    derive_recovery_wrap_key,
    normalize_vault_id,
)
from .canonical import _now_rfc3339
from .ids import vault_id_dashed


def recovery_kit_path(config_dir, vault_id: str):
    """Resolve the on-disk path for a vault's recovery kit per
    formats §12.5: ``<vault-id-with-dashes>.dc-vault-recovery``.
    """
    return Path(config_dir) / f"{vault_id_dashed(vault_id)}.dc-vault-recovery"


def recovery_envelope_meta_to_json(meta: dict) -> dict:
    """Serialize non-secret recovery-envelope metadata for config.json."""
    return {
        "envelope_id": str(meta["envelope_id"]),
        "argon_salt_b64": base64.b64encode(meta["argon_salt"]).decode("ascii"),
        "argon_memory_kib": int(meta["argon_memory_kib"]),
        "argon_iterations": int(meta["argon_iterations"]),
        "nonce_b64": base64.b64encode(meta["nonce"]).decode("ascii"),
        "aead_ciphertext_and_tag_b64": base64.b64encode(
            meta["aead_ciphertext_and_tag"]
        ).decode("ascii"),
    }


def recovery_envelope_meta_from_json(data: dict) -> dict:
    """Deserialize config.json recovery-envelope metadata."""
    if not isinstance(data, dict):
        raise ValueError("recovery test metadata is missing")
    return {
        "envelope_id": str(data["envelope_id"]),
        "argon_salt": base64.b64decode(data["argon_salt_b64"]),
        "argon_memory_kib": int(data["argon_memory_kib"]),
        "argon_iterations": int(data["argon_iterations"]),
        "nonce": base64.b64decode(data["nonce_b64"]),
        "aead_ciphertext_and_tag": base64.b64decode(
            data["aead_ciphertext_and_tag_b64"]
        ),
    }


def write_recovery_kit_file(
    path,
    *,
    vault_id: str,
    recovery_secret: bytes,
    vault_access_secret: str,
    recovery_envelope_meta: dict | None = None,
    created_at: str | None = None,
) -> None:
    """Persist the recovery kit per formats §12.5.

    Writes a plaintext UTF-8 file (LF line endings, mode 0o600)
    containing every piece of state a fresh device needs to recover:

      - ``vault_id``         — which vault on the relay to fetch.
      - ``recovery_secret``  — 32 random bytes; the "kit" half of the
                                two-factor unlock (passphrase is the other).
      - ``vault_access_secret`` — bearer for ``X-Vault-Authorization``,
                                   needed to fetch the encrypted header
                                   from the relay during recovery.
      - ``argon_params``     — locked at v1 (argon2id-v1).

    The file is **not encrypted at rest** — security comes from physical
    custody (USB drive, password-manager attachment, paper in a safe)
    *plus* the user's passphrase. An attacker who steals only the kit
    file still has to brute-force the user's passphrase against
    Argon2id (m=128 MiB, t=4) to derive the master key. An attacker
    who steals the relay's bytes but not the kit gets nothing — the
    relay never sees a kit file.

    Caller must persist this BEFORE ``Vault.close()`` zeros the
    in-memory ``recovery_secret`` buffer.
    """
    if len(recovery_secret) != 32:
        raise ValueError(f"recovery_secret must be 32 bytes; got {len(recovery_secret)}")
    if not vault_access_secret:
        raise ValueError("vault_access_secret is required")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    secret_b32 = base64.b32encode(recovery_secret).decode("ascii").lower().rstrip("=")
    if created_at is None:
        created_at = _now_rfc3339()

    body = (
        "# Desktop Connector — Vault Recovery Kit\n"
        f"# Vault ID: {vault_id_dashed(vault_id)}\n"
        f"# Created:  {created_at}\n"
        "#\n"
        "# This file PLUS your recovery passphrase can restore the vault\n"
        "# on a new device. BOTH are required. Lose either, and the vault\n"
        "# cannot be recovered — there is no password reset.\n"
        "#\n"
        "# Keep this file somewhere safe and offline — a USB drive, a password\n"
        "# manager attachment, or printed and stored in a safe. The relay\n"
        "# server is NOT a backup; if it's lost or wiped, this file is your\n"
        "# only path back.\n"
        "\n"
        f"vault_id: {vault_id_dashed(vault_id)}\n"
        f"created_at: {created_at}\n"
        f"recovery_secret: {secret_b32}\n"
        f"vault_access_secret: {vault_access_secret}\n"
        "argon_params: argon2id-v1\n"
    )
    if recovery_envelope_meta is not None:
        encoded_meta = recovery_envelope_meta_to_json(recovery_envelope_meta)
        body += (
            f"recovery_envelope_id: {encoded_meta['envelope_id']}\n"
            f"recovery_argon_salt: {encoded_meta['argon_salt_b64']}\n"
            f"recovery_argon_memory_kib: {encoded_meta['argon_memory_kib']}\n"
            f"recovery_argon_iterations: {encoded_meta['argon_iterations']}\n"
            f"recovery_envelope_nonce: {encoded_meta['nonce_b64']}\n"
            "recovery_envelope_ciphertext: "
            f"{encoded_meta['aead_ciphertext_and_tag_b64']}\n"
        )
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def parse_recovery_kit_file(path) -> dict:
    """Parse a kit file written by :func:`write_recovery_kit_file`.

    Returns a dict with:
        ``vault_id`` (str, 12-char canonical undashed),
        ``vault_id_dashed`` (str, 4-4-4 display form),
        ``recovery_secret`` (bytes, 32),
        ``vault_access_secret`` (str),
        ``argon_params`` (str — the ``argon2id-v1`` tag).

    Raises ``ValueError`` if any required field is missing or malformed.
    Tolerant to upper/lower case in ``recovery_secret`` per formats §12.5.
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"malformed kit line: {raw!r}")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()

    for required in ("vault_id", "recovery_secret", "vault_access_secret", "argon_params"):
        if required not in fields:
            raise ValueError(f"recovery kit missing required field: {required}")

    raw_b32 = fields["recovery_secret"].upper()
    pad = (8 - len(raw_b32) % 8) % 8
    try:
        recovery_secret = base64.b32decode(raw_b32 + "=" * pad)
    except Exception as exc:
        raise ValueError(f"recovery_secret is not valid base32: {exc}") from exc
    if len(recovery_secret) != 32:
        raise ValueError(f"recovery_secret decodes to {len(recovery_secret)} bytes; expected 32")

    parsed = {
        "vault_id": normalize_vault_id(fields["vault_id"]),
        "vault_id_dashed": vault_id_dashed(normalize_vault_id(fields["vault_id"])),
        "recovery_secret": recovery_secret,
        "vault_access_secret": fields["vault_access_secret"],
        "argon_params": fields["argon_params"],
    }
    meta_fields = {
        "recovery_envelope_id",
        "recovery_argon_salt",
        "recovery_argon_memory_kib",
        "recovery_argon_iterations",
        "recovery_envelope_nonce",
        "recovery_envelope_ciphertext",
    }
    if meta_fields.intersection(fields):
        missing = sorted(meta_fields - set(fields))
        if missing:
            raise ValueError(
                "recovery kit has incomplete recovery test metadata: "
                + ", ".join(missing)
            )
        parsed["recovery_envelope_meta"] = recovery_envelope_meta_from_json({
            "envelope_id": fields["recovery_envelope_id"],
            "argon_salt_b64": fields["recovery_argon_salt"],
            "argon_memory_kib": fields["recovery_argon_memory_kib"],
            "argon_iterations": fields["recovery_argon_iterations"],
            "nonce_b64": fields["recovery_envelope_nonce"],
            "aead_ciphertext_and_tag_b64": fields["recovery_envelope_ciphertext"],
        })
    return parsed


def verify_recovery_kit(
    kit_path,
    *,
    passphrase: str,
    envelope_meta: dict,
) -> tuple[bool, str]:
    """Re-run the recovery flow against a saved kit + the user's
    passphrase. Returns ``(ok, message)``.

    This is the **real** recovery test the wizard runs after the user
    exports their kit: it parses the kit file from disk, re-derives
    ``wrap_key`` from passphrase + ``recovery_secret`` exactly the way
    a future "I'm on a new device" recovery would, and tries to
    AEAD-decrypt the recovery envelope (whose ciphertext we wrap the
    master key inside at create time, exposed via
    :attr:`Vault.recovery_envelope_meta`).

    If the AEAD decryption succeeds, the kit + passphrase combination
    can produce the master key — recovery will work. If Poly1305
    verification fails (wrong passphrase typed, kit file edited,
    bytes corrupted), AEAD raises and we return ``(False, …)``.
    """
    try:
        parsed = parse_recovery_kit_file(kit_path)
    except (OSError, ValueError) as exc:
        return False, f"Could not parse kit file: {exc}"

    # Review §2.L4 — narrowed from a bare ``except Exception`` to the
    # two specific failure modes that signal "user can't recover":
    # ``CryptoError`` (Poly1305 tag mismatch → wrong passphrase OR
    # corrupted kit) and ``KeyError`` (envelope_meta missing a field
    # the AEAD path needs → caller bug). Pre-fix the bare except also
    # swallowed real OS errors (disk read failure on the kit file's
    # subsequent re-read, malloc failure inside Argon2id) as "wrong
    # passphrase", which left the user staring at a misleading error
    # while a deeper problem festered.
    import nacl.exceptions
    try:
        wrap_key = derive_recovery_wrap_key(
            passphrase=passphrase,
            recovery_secret=parsed["recovery_secret"],
            argon_salt=envelope_meta["argon_salt"],
            memory_kib=int(envelope_meta["argon_memory_kib"]),
            iterations=int(envelope_meta["argon_iterations"]),
        )
        aad = build_recovery_aad(
            parsed["vault_id"],
            envelope_meta["envelope_id"],
        )
        aead_decrypt(
            envelope_meta["aead_ciphertext_and_tag"],
            wrap_key,
            envelope_meta["nonce"],
            aad,
        )
    except (nacl.exceptions.CryptoError, KeyError) as exc:
        return False, f"Recovery test failed: {type(exc).__name__}"
    # Everything else (OSError on backing storage, etc.) propagates so
    # the caller sees the real failure mode.

    return True, "kit + passphrase produce the correct master key"


def shred_file(path) -> bool:
    """Best-effort secure delete: overwrite the file with random bytes,
    fsync, then unlink.

    Returns ``True`` if the file was overwritten + removed, ``False`` if
    it didn't exist or the operation hit an OSError. Intentionally
    swallows IO errors so the wizard's Done button can't fail because
    the user moved the file between Export and Done.

    Caveat — on modern SSDs with wear leveling, the OS may have written
    copies to spare blocks we can't reach. This is best-effort cleanup
    suitable for "I already copied it into a password manager, now make
    sure it's not just sitting in Downloads"; users who need true
    deletion should rely on full-disk encryption + secure-erase at
    decommission time.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return False
    try:
        size = p.stat().st_size
        # Two passes: random, then zeros. More is theatre on SSD; this
        # at least covers the obvious filesystem-cache + on-disk paths.
        with open(p, "r+b") as f:
            for fill in (os.urandom(size), b"\x00" * size):
                f.seek(0)
                f.write(fill)
                f.flush()
                os.fsync(f.fileno())
        p.unlink()
        return True
    except OSError:
        return False
