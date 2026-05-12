"""Cross-session resume of an unfinished vault create.

The wizard's three-phase order (prepare → save_grant → publish → save_config)
sequences side-effects so a crash never leaves the relay in a state the
local config doesn't know about. The 2026-05-07 fix folded phases 3 + 4
into a single worker call, shrinking the in-session orphan window to
microseconds. Cross-session orphans — published rows from a wizard run
that was abandoned before the config commit — still leak.

This module closes that hole without server changes. After
``save_local_vault_grant`` returns, ``set_pending_publish_marker`` records
the vault id (and originating server URL) in ``config.json`` under
``vault.pending_publish``. The marker is cleared in the same
``config.save()`` call that writes ``last_known_id`` + recovery envelope
metadata on success. If the user opens the wizard later and the marker is
still present, the wizard offers Resume or Discard.

Resume re-derives fresh recovery material from the same master key (read
from the local grant) plus a user-typed passphrase, then either updates
the orphaned relay row's header (``PUT /api/vaults/{id}/header``) or
publishes a brand-new row under the existing vault id (``POST
/api/vaults``) depending on whether the relay still has the row. Either
way the success screen ends up with a fully-formed Vault state — a fresh
recovery secret + envelope the user can immediately back up.

Discard removes the local grant and clears the marker; the orphan stays
on the relay as harmless ciphertext (no decryption key, no user has its
recovery kit) and is collected by the relay's retention policy.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from .canonical import _canonical_json, _now_rfc3339
from .crypto import (
    aead_decrypt,
    aead_encrypt,
    build_header_aad,
    build_header_envelope,
    build_manifest_aad,
    build_manifest_envelope,
    build_recovery_aad,
    derive_recovery_wrap_key,
    derive_subkey,
    normalize_vault_id,
)
from .ids import _generate_id_v1, _genesis_fingerprint_hex
from .manifest import canonical_manifest_json, make_manifest

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- marker helpers


def set_pending_publish_marker(
    config,
    vault_id: str,
    server_url: str,
    *,
    now_provider=None,
) -> None:
    """Record that this device has saved a vault grant but may not have
    successfully recorded the relay publish in ``config.json`` yet.

    Written immediately after :func:`save_local_vault_grant` returns and
    cleared by :func:`clear_pending_publish_marker` once
    ``config.vault.last_known_id`` is durable.
    """
    if not isinstance(config._data.get("vault"), dict):
        config._data["vault"] = {}
    config._data["vault"]["pending_publish"] = {
        "vault_id": normalize_vault_id(vault_id),
        "server_url": server_url,
        "created_at": (now_provider or _now_rfc3339)(),
    }
    config.save()


def read_pending_publish_marker(config) -> dict | None:
    """Return the marker dict (with ``vault_id``, ``server_url``,
    ``created_at``) if a prior wizard session left one, else ``None``.
    """
    vault_block = config._data.get("vault")
    if not isinstance(vault_block, dict):
        return None
    marker = vault_block.get("pending_publish")
    if not isinstance(marker, dict):
        return None
    raw_id = marker.get("vault_id")
    if not isinstance(raw_id, str) or len(raw_id) == 0:
        return None
    return {
        "vault_id": normalize_vault_id(raw_id),
        "server_url": str(marker.get("server_url", "")),
        "created_at": str(marker.get("created_at", "")),
    }


def clear_pending_publish_marker(config) -> None:
    """Remove the marker from config — does NOT call ``config.save()``.

    Callers fold the clear into a single ``config.save()`` alongside
    other commits (``last_known_id``, ``recovery_envelope_meta``) so the
    transition is atomic on disk.
    """
    vault_block = config._data.get("vault")
    if isinstance(vault_block, dict):
        vault_block.pop("pending_publish", None)


# ---------------------------------------------------------------- discard


def discard_pending_publish(config_dir: Path, config, vault_id: str) -> None:
    """Drop the local grant artifacts for an abandoned vault and clear
    the marker.

    The relay row (if any) stays. It cannot be decrypted by anyone — no
    device holds the master key after the grant deletion. The relay's
    retention policy is responsible for eventually GCing it.
    """
    from .grant.store import delete_local_grant_artifacts

    canonical = normalize_vault_id(vault_id)
    log.info("vault.resume.discard.start vault=%s", canonical[:8] + "…")
    delete_local_grant_artifacts(Path(config_dir), canonical)
    clear_pending_publish_marker(config)
    config.save()
    log.info("vault.resume.discard.ok vault=%s", canonical[:8] + "…")


# ---------------------------------------------------------------- complete


@dataclass
class ResumedVaultState:
    """Fields the success screen needs to surface a Resume-completed vault.

    Shape mirrors the wizard's ``state`` dict so the GTK code can do a
    1-to-1 assignment without any further conversion.
    """
    vault_id: str
    vault_access_secret: str
    recovery_secret_bytes: bytes
    recovery_envelope_meta: dict


def complete_pending_publish(
    config_dir: Path,
    config,
    vault_id: str,
    recovery_passphrase: str,
    *,
    relay=None,
    grant_loader=None,
    argon_memory_kib: int = 131_072,
    argon_iterations: int = 4,
    now_provider=None,
) -> ResumedVaultState:
    """Finish an abandoned vault create.

    Reads the existing grant for ``vault_id`` to recover the master key,
    builds fresh recovery material (a new ``recovery_secret`` + envelope)
    keyed on ``recovery_passphrase``, then either updates the relay row's
    header (if the row exists) or creates the row (if it doesn't). The
    config is rewritten with ``last_known_id`` + recovery envelope
    metadata, and the pending-publish marker is cleared in the same save.

    Returns a :class:`ResumedVaultState` so the wizard can populate its
    success-screen state — Export + Verify on the success screen works
    exactly as if the user had just finished a fresh ``Create new vault``
    flow.

    ``relay`` defaults to the production HTTP relay; tests inject a fake.
    ``grant_loader`` is a test seam — a callable returning the
    ``(master_key, vault_access_secret)`` tuple read from the local
    grant. Defaults to :func:`open_local_vault_from_grant`.
    """
    from .recovery_kit import recovery_envelope_meta_to_json

    canonical = normalize_vault_id(vault_id)
    log.info("vault.resume.complete.start vault=%s", canonical[:8] + "…")

    if relay is None:
        from .binding.runtime import create_vault_relay
        relay = create_vault_relay(config)

    if grant_loader is None:
        from .binding.runtime import open_local_vault_from_grant
        def _default_loader():
            vault = open_local_vault_from_grant(config_dir, config, canonical)
            try:
                mk = vault.master_key
                vs = vault.vault_access_secret
                if mk is None or vs is None:
                    raise RuntimeError(
                        "Local vault material was unavailable while completing "
                        "the previous publish. Discard and start a fresh setup."
                    )
                return bytes(mk), vs
            finally:
                vault.close()
        grant_loader = _default_loader

    master_key_bytes, vault_access_secret = grant_loader()
    if not isinstance(master_key_bytes, bytes) or len(master_key_bytes) != 32:
        raise RuntimeError("grant_loader returned an invalid master key")
    if not isinstance(vault_access_secret, str) or not vault_access_secret:
        raise RuntimeError("grant_loader returned an invalid vault access secret")

    recovery_secret, header_envelope, header_hash, header_plaintext_meta = (
        _build_resume_header(
            vault_id=canonical,
            master_key=master_key_bytes,
            recovery_passphrase=recovery_passphrase,
            header_revision=1,
            argon_memory_kib=argon_memory_kib,
            argon_iterations=argon_iterations,
            now_provider=now_provider,
        )
    )

    relay_state = _probe_relay_state(relay, canonical, vault_access_secret)

    if relay_state["exists"]:
        new_revision = int(relay_state["header_revision"]) + 1
        # PUT-header needs the AAD bound to the NEW revision, not 1 —
        # rebuild the envelope at the right revision.
        new_header_envelope, new_header_hash = _seal_header(
            vault_id=canonical,
            master_key=master_key_bytes,
            header_revision=new_revision,
            header_plaintext_bytes=header_plaintext_meta["plaintext_bytes"],
        )
        _put_header(
            relay,
            canonical,
            vault_access_secret,
            expected_header_revision=int(relay_state["header_revision"]),
            new_header_revision=new_revision,
            encrypted_header=new_header_envelope,
            header_hash=new_header_hash,
        )
        log.info(
            "vault.resume.put_header.ok vault=%s rev=%d",
            canonical[:8] + "…", new_revision,
        )
    else:
        manifest_envelope, manifest_hash = _build_genesis_manifest(
            vault_id=canonical,
            master_key=master_key_bytes,
            now_provider=now_provider,
        )
        token_hash = hashlib.sha256(
            vault_access_secret.encode("ascii"),
        ).digest()
        relay.create_vault(
            vault_id=canonical,
            vault_access_token_hash=token_hash,
            encrypted_header=header_envelope,
            header_hash=header_hash,
            initial_manifest_ciphertext=manifest_envelope,
            initial_manifest_hash=manifest_hash,
        )
        log.info("vault.resume.create.ok vault=%s", canonical[:8] + "…")

    recovery_envelope_meta = header_plaintext_meta["recovery_envelope_meta"]

    if not isinstance(config._data.get("vault"), dict):
        config._data["vault"] = {}
    config._data["vault"]["last_known_id"] = canonical
    config._data["vault"]["recovery_envelope_meta"] = (
        recovery_envelope_meta_to_json(recovery_envelope_meta)
    )
    clear_pending_publish_marker(config)
    config.save()

    log.info("vault.resume.complete.ok vault=%s", canonical[:8] + "…")
    return ResumedVaultState(
        vault_id=canonical,
        vault_access_secret=vault_access_secret,
        recovery_secret_bytes=recovery_secret,
        recovery_envelope_meta=recovery_envelope_meta,
    )


# ---------------------------------------------------------------- internals


def _probe_relay_state(relay, vault_id: str, vault_access_secret: str) -> dict:
    """Return ``{"exists": bool, "header_revision": int|None}`` for a vault
    id on the relay.

    Distinguishes "vault row missing" (404 / not-found error) from
    transport failure (re-raised). The HTTP relay raises generic
    ``RuntimeError("Relay rejected ... HTTP 404 ...")`` for 404s; the
    fake relay in tests raises ``KeyError``. Both are detected.
    """
    try:
        header_resp = relay.get_header(vault_id, vault_access_secret)
    except KeyError:
        return {"exists": False, "header_revision": None}
    except RuntimeError as exc:
        message = str(exc)
        if "404" in message or "vault_not_found" in message.lower():
            return {"exists": False, "header_revision": None}
        raise
    if not isinstance(header_resp, dict):
        raise RuntimeError("Relay returned an invalid header response on probe.")
    return {
        "exists": True,
        "header_revision": int(header_resp.get("header_revision", 1)),
    }


def _build_resume_header(
    *,
    vault_id: str,
    master_key: bytes,
    recovery_passphrase: str,
    header_revision: int,
    argon_memory_kib: int,
    argon_iterations: int,
    now_provider=None,
):
    """Build a fresh header envelope keyed on the existing master key.

    Returns ``(recovery_secret_bytes, header_envelope, header_hash,
    {"plaintext_bytes": …, "recovery_envelope_meta": {…}})``. The caller
    re-seals at a different ``header_revision`` for the PUT-header path by
    re-running :func:`_seal_header` on the same ``plaintext_bytes``.
    """
    recovery_secret = secrets.token_bytes(32)
    recovery_envelope_id = _generate_id_v1("rk")
    recovery_argon_salt = secrets.token_bytes(16)

    recovery_wrap_key = derive_recovery_wrap_key(
        passphrase=recovery_passphrase,
        recovery_secret=recovery_secret,
        argon_salt=recovery_argon_salt,
        memory_kib=argon_memory_kib,
        iterations=argon_iterations,
    )
    recovery_envelope_nonce = secrets.token_bytes(24)
    recovery_envelope_ct = aead_encrypt(
        master_key,
        recovery_wrap_key,
        recovery_envelope_nonce,
        build_recovery_aad(vault_id, recovery_envelope_id),
    )

    header_plaintext_bytes = _canonical_json({
        "schema": "dc-vault-header-v1",
        "vault_id": vault_id,
        "created_at": (now_provider or _now_rfc3339)(),
        "genesis_fingerprint": _genesis_fingerprint_hex(master_key),
        "kdf_profiles": {"recovery": "argon2id-v1", "export": "argon2id-v1"},
        "recovery_envelopes": [
            {
                "envelope_id": recovery_envelope_id,
                "type": "recovery-kit-passphrase",
                "argon_salt": base64.b64encode(recovery_argon_salt).decode("ascii"),
                "argon_params": {
                    "memory_kib": argon_memory_kib,
                    "iterations": argon_iterations,
                    "parallelism": 1,
                },
                "nonce": base64.b64encode(recovery_envelope_nonce).decode("ascii"),
                "aead_ciphertext_and_tag": base64.b64encode(recovery_envelope_ct).decode("ascii"),
            }
        ],
        "manifest_format_version": 1,
        "header_format_version": 1,
    })

    header_envelope, header_hash = _seal_header(
        vault_id=vault_id,
        master_key=master_key,
        header_revision=header_revision,
        header_plaintext_bytes=header_plaintext_bytes,
    )

    recovery_envelope_meta = {
        "envelope_id": recovery_envelope_id,
        "argon_salt": recovery_argon_salt,
        "argon_memory_kib": argon_memory_kib,
        "argon_iterations": argon_iterations,
        "nonce": recovery_envelope_nonce,
        "aead_ciphertext_and_tag": recovery_envelope_ct,
    }
    return recovery_secret, header_envelope, header_hash, {
        "plaintext_bytes": header_plaintext_bytes,
        "recovery_envelope_meta": recovery_envelope_meta,
    }


def _seal_header(
    *,
    vault_id: str,
    master_key: bytes,
    header_revision: int,
    header_plaintext_bytes: bytes,
):
    """AEAD-seal a header plaintext at a given revision.

    Separate from plaintext construction so the PUT-header path can
    re-seal at a different revision without re-deriving recovery material
    (and so the same recovery secret is the one the user has to back up).
    """
    header_subkey = derive_subkey("dc-vault-v1/header", master_key)
    header_nonce = secrets.token_bytes(24)
    header_aad = build_header_aad(vault_id, header_revision=header_revision)
    header_ciphertext = aead_encrypt(
        header_plaintext_bytes, header_subkey, header_nonce, header_aad,
    )
    header_envelope = build_header_envelope(
        vault_id=vault_id,
        header_revision=header_revision,
        nonce=header_nonce,
        aead_ciphertext_and_tag=header_ciphertext,
    )
    header_hash = hashlib.sha256(header_envelope).hexdigest()
    return header_envelope, header_hash


def _build_genesis_manifest(
    *,
    vault_id: str,
    master_key: bytes,
    now_provider=None,
):
    """Return ``(manifest_envelope_bytes, manifest_hash_hex)`` — fresh
    genesis manifest at revision 1 with author_device_id zero.

    Mirrors the shape ``Vault.prepare_new`` builds for the POST. Only used
    on the resume-via-POST path (relay 404'd on get_header).
    """
    author_device_id = "0" * 32
    manifest_plaintext = canonical_manifest_json(make_manifest(
        vault_id=vault_id,
        revision=1,
        parent_revision=0,
        created_at=(now_provider or _now_rfc3339)(),
        author_device_id=author_device_id,
        remote_folders=[],
        operation_log_tail=[],
        archived_op_segments=[],
    ))
    manifest_subkey = derive_subkey("dc-vault-v1/manifest", master_key)
    manifest_nonce = secrets.token_bytes(24)
    manifest_aad = build_manifest_aad(
        vault_id=vault_id, revision=1, parent_revision=0,
        author_device_id=author_device_id,
    )
    manifest_ciphertext = aead_encrypt(
        manifest_plaintext, manifest_subkey, manifest_nonce, manifest_aad,
    )
    manifest_envelope = build_manifest_envelope(
        vault_id=vault_id, revision=1, parent_revision=0,
        author_device_id=author_device_id,
        nonce=manifest_nonce,
        aead_ciphertext_and_tag=manifest_ciphertext,
    )
    manifest_hash = hashlib.sha256(manifest_envelope).hexdigest()
    return manifest_envelope, manifest_hash


def _put_header(
    relay,
    vault_id: str,
    vault_access_secret: str,
    *,
    expected_header_revision: int,
    new_header_revision: int,
    encrypted_header: bytes,
    header_hash: str,
) -> None:
    """Invoke ``relay.put_header(...)`` on the relay adapter.

    The HTTP adapter implements this against ``PUT /api/vaults/{id}/header``
    (CAS). The protocol is narrow on purpose: resume is the only caller.
    """
    relay.put_header(
        vault_id,
        vault_access_secret,
        expected_header_revision=expected_header_revision,
        new_header_revision=new_header_revision,
        encrypted_header=encrypted_header,
        header_hash=header_hash,
    )


__all__ = [
    "ResumedVaultState",
    "clear_pending_publish_marker",
    "complete_pending_publish",
    "discard_pending_publish",
    "read_pending_publish_marker",
    "set_pending_publish_marker",
]
