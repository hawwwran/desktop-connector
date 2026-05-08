"""Local-only Vault state transitions.

These helpers do not talk to the relay. They mutate this machine's
connection to a vault: local metadata, local unlock grants, and pending
local state files.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from .vault_grant import delete_local_grant_artifacts


log = logging.getLogger(__name__)
GrantDeleter = Callable[[Path, str], None]


def _purge_cached_resume_state(vault_id: str) -> None:
    """F-D26: drop upload resume sessions + per-vault chunk cache after
    disconnect.

    Without this, two failure modes survive:

    1. **Stale upload sessions.** Resume metadata at
       ``<XDG_CACHE_HOME>/desktop-connector/vault/uploads/<id>.json``
       is keyed on ``session.vault_id``. Disconnect-then-reconnect to a
       *different* vault leaves the old sessions intact. Worse:
       reconnecting to a vault that happens to share the disconnected
       one's id (re-import from a recovery kit, or another device
       publishing under the same id after a relay-side wipe) would
       resurrect the dead sessions and try to PUT chunks against the
       new vault. The relay's ciphertext-CAS would catch the mismatch
       at the per-chunk hash check, but the resumed upload would
       still leak ``local_path`` and chunk-id metadata to disk that
       no longer maps to anything reachable.
    2. **Per-vault chunk cache drag.** ``vault_chunk_cache_path``
       writes encrypted chunks to
       ``<cache>/desktop-connector/vault/chunks/<vault_id_normalized>/``;
       eviction never visits this directory after a disconnect. The
       chunks are AEAD-bound to the disconnected vault's master key
       (so a casual attacker with disk read can't decrypt) but the
       leak surface is real: file sizes + counts reveal vault
       activity over time, and a future re-import via recovery-kit
       could pair with these chunks if the relay also still carried
       them.

    The purge is best-effort — failures log a warning but don't block
    disconnect. The grant-artifacts deletion (`delete_local_grant_artifacts`)
    is the load-bearing path; the cache cleanup is hygiene on top.
    """
    # Local imports keep ``vault_upload`` / ``vault_download`` out of
    # the import graph for callers that just want disconnect — those
    # modules pull in the AEAD + atomic-write helpers transitively.
    from .vault_crypto import normalize_vault_id
    from .vault_upload import default_upload_resume_dir, UploadSession
    from .vault_download import default_vault_download_cache_dir

    # 1. Per-vault upload resume sessions (filter by session.vault_id
    # so we don't nuke another vault's sessions if both happen to be
    # cached).
    resume_dir = default_upload_resume_dir()
    if resume_dir.exists():
        try:
            target_id = normalize_vault_id(vault_id)
        except ValueError:
            target_id = vault_id
        for session_path in sorted(resume_dir.glob("*.json")):
            try:
                with open(session_path, "rb") as fh:
                    data = json.loads(fh.read().decode("utf-8"))
                session = UploadSession.from_json(data)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                # Corrupt JSON or schema drift — leave it alone; the
                # next list_resumable_sessions call will skip it too.
                continue
            try:
                session_vault = normalize_vault_id(session.vault_id)
            except ValueError:
                session_vault = session.vault_id
            if session_vault != target_id:
                continue
            try:
                session_path.unlink()
                log.info(
                    "vault_local_state.disconnect_dropped_resume_session "
                    "session=%s vault=%s",
                    session.session_id, vault_id,
                )
            except OSError as exc:
                log.warning(
                    "vault_local_state.disconnect_resume_session_unlink_failed "
                    "session=%s error=%s",
                    session.session_id, exc,
                )

    # 2. Per-vault chunk cache. The path layout is
    # ``<base>/chunks/<normalized_vault_id>/`` — one tree per vault.
    cache_base = default_vault_download_cache_dir()
    try:
        normalized_id = normalize_vault_id(vault_id)
    except ValueError:
        normalized_id = vault_id
    chunk_root = cache_base / "chunks" / normalized_id
    if chunk_root.exists():
        try:
            shutil.rmtree(chunk_root)
            log.info(
                "vault_local_state.disconnect_chunk_cache_purged vault=%s",
                vault_id,
            )
        except OSError as exc:
            log.warning(
                "vault_local_state.disconnect_chunk_cache_purge_failed "
                "vault=%s error=%s",
                vault_id, exc,
            )


@dataclass(frozen=True)
class RecoveryMaterialTestResult:
    ok: bool
    message: str
    wiped: bool = False


def disconnect_local_vault(
    config,
    *,
    grant_deleter: GrantDeleter = delete_local_grant_artifacts,
) -> str | None:
    """Forget the connected vault on this machine only.

    Returns the disconnected vault id when one was known. The Vault
    active toggle is preserved so the next Settings/Tray action routes
    to create/import instead of hiding the feature.
    """
    config.reload()
    raw = config._data.get("vault")
    vault_meta = raw if isinstance(raw, dict) else {}
    vault_id = vault_meta.get("last_known_id")
    active = bool(vault_meta.get("active", True))

    if isinstance(vault_id, str) and vault_id:
        grant_deleter(Path(config.config_dir), vault_id)

    config._data["vault"] = {"active": active}
    config.save()

    for name in ("vault_migration.json", "vault_pending_purges.json"):
        try:
            (Path(config.config_dir) / name).unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("vault_local_state.disconnect_state_file_delete_failed file=%s error=%s", name, exc)

    # F-D26: scrub upload resume sessions + per-vault chunk cache so a
    # later reconnect (same id or different) doesn't resurrect dead
    # state. Only runs when we knew about a vault to begin with.
    if isinstance(vault_id, str) and vault_id:
        try:
            _purge_cached_resume_state(vault_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault_local_state.disconnect_cache_purge_failed vault=%s error=%s",
                vault_id, exc,
            )

    return vault_id if isinstance(vault_id, str) and vault_id else None


def run_recovery_material_test(
    kit_path,
    *,
    passphrase: str,
    vault_id: str,
    envelope_meta: dict | None = None,
    wipe_after_success: bool = False,
) -> RecoveryMaterialTestResult:
    """Verify a recovery kit file, passphrase, and Vault ID together."""
    from .vault import (
        normalize_vault_id,
        parse_recovery_kit_file,
        shred_file,
        vault_id_dashed,
        verify_recovery_kit,
    )

    if not str(kit_path or "").strip():
        return RecoveryMaterialTestResult(False, "Choose a recovery kit file first.")
    if not passphrase:
        return RecoveryMaterialTestResult(False, "Enter the recovery passphrase.")
    try:
        expected_vault_id = normalize_vault_id(vault_id)
    except ValueError as exc:
        return RecoveryMaterialTestResult(False, f"Vault ID is not valid: {exc}")

    try:
        parsed = parse_recovery_kit_file(kit_path)
    except (OSError, ValueError) as exc:
        return RecoveryMaterialTestResult(False, f"Could not read recovery kit: {exc}")

    if parsed["vault_id"] != expected_vault_id:
        return RecoveryMaterialTestResult(
            False,
            "Vault ID mismatch: kit is for "
            f"{parsed['vault_id_dashed']}, entered {vault_id_dashed(expected_vault_id)}.",
        )

    meta = envelope_meta or parsed.get("recovery_envelope_meta")
    if not isinstance(meta, dict):
        return RecoveryMaterialTestResult(
            False,
            "This recovery kit is the old incomplete format. It is missing the "
            "recovery envelope needed to verify the passphrase, so this build "
            "cannot prove that the kit can recover the vault. Create a new vault "
            "with the current version and export a new recovery kit.",
        )

    ok, msg = verify_recovery_kit(
        kit_path,
        passphrase=passphrase,
        envelope_meta=meta,
    )
    if not ok:
        return RecoveryMaterialTestResult(False, msg)

    if not wipe_after_success:
        return RecoveryMaterialTestResult(True, msg)

    if shred_file(kit_path):
        return RecoveryMaterialTestResult(
            True,
            msg + ". The recovery kit file was securely deleted.",
            wiped=True,
        )
    return RecoveryMaterialTestResult(
        False,
        msg + ", but the recovery kit file could not be securely deleted.",
    )
