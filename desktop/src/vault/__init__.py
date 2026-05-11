"""Public surface of the vault domain class + recovery-kit helpers.

Composed from topical submodules under this package; legacy
``from .vault import Vault`` / ``from src.vault import …`` imports
keep working unchanged because Python resolves ``vault`` as this
package and finds the names below in the package namespace.

``normalize_vault_id`` is re-exported from ``vault_crypto`` because
``vault_local_state.py`` imports it through this module path.
"""

from .crypto import normalize_vault_id
from .ids import (
    _BASE32_LOWER,
    _BASE32_UPPER,
    _generate_id_v1,
    _generate_vault_id,
    _genesis_fingerprint_hex,
    vault_id_dashed,
)
from .canonical import _canonical_json, _now_rfc3339
from .protocols import RelayProtocol
from .recovery_kit import (
    parse_recovery_kit_file,
    recovery_envelope_meta_from_json,
    recovery_envelope_meta_to_json,
    recovery_kit_path,
    shred_file,
    verify_recovery_kit,
    write_recovery_kit_file,
)
from .vault import VAULT_CHUNK_SIZE, Vault

__all__ = [
    "RelayProtocol",
    "VAULT_CHUNK_SIZE",
    "Vault",
    "normalize_vault_id",
    "parse_recovery_kit_file",
    "recovery_envelope_meta_from_json",
    "recovery_envelope_meta_to_json",
    "recovery_kit_path",
    "shred_file",
    "vault_id_dashed",
    "verify_recovery_kit",
    "write_recovery_kit_file",
]
