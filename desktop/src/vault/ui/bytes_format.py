"""Human-readable byte formatter shared across Vault UI surfaces.

F-514: there were two near-identical helpers — one in
``vault_binding_preflight`` (binary divisions, decimal labels, capped
at TB) and one in ``vault_folder_ui_state`` (binary divisions,
decimal labels, capped at MB so a 5 GB vault rendered as
"5120.0 MB"). Pick one shape and reuse it.

Convention: divide by 1024, label as KiB / MiB / GiB / TiB. Matches
the spec's "ciphertext size on disk" sense — relay storage is
allocated in 4 KiB sectors, not 1 KB ones, and the user is reading
this value off disk-usage tools that already speak binary.
"""

from __future__ import annotations

_UNITS_BINARY = ("B", "KiB", "MiB", "GiB", "TiB")


def format_bytes_binary(value: int) -> str:
    """Return a binary-units (KiB/MiB/GiB/TiB) display for a byte count.

    Caps at TiB; values past 1024 TiB still render as TiB with a
    larger leading number rather than rolling over to PiB (a Vault at
    that scale has bigger problems than display rounding).
    """
    size = max(0, int(value))
    amount = float(size)
    unit = _UNITS_BINARY[0]
    for unit in _UNITS_BINARY:
        if amount < 1024 or unit == _UNITS_BINARY[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    return f"{amount:.1f} {unit}"


__all__ = ["format_bytes_binary"]
