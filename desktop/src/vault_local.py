"""Local-only Vault state transitions.

These helpers do not talk to the relay. They mutate this machine's
connection to a vault: local metadata, local unlock grants, and pending
local state files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from .vault_grant import delete_local_grant_artifacts


log = logging.getLogger(__name__)
GrantDeleter = Callable[[Path, str], None]


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
            log.warning("vault_local.disconnect_state_file_delete_failed file=%s error=%s", name, exc)

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
