"""Shared context threaded into each tab builder of the vault settings window.

The pre-split ``show_vault_main`` captured ``config``, ``config_dir``,
``vault_id_undashed``, ``log`` and a ``vault_id_dashed`` closure as locals
inside one giant ``on_activate``. Splitting tabs into sibling modules
turns those captures into named attributes on this small holder so each
``build_*_tab`` function gets exactly what it needs without re-importing.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging

    from ..config import Config


@dataclass
class MainContext:
    app: Any
    config: "Config"
    config_dir: Path
    vault_id_undashed: str
    log: "logging.Logger"

    def vault_id_dashed(self) -> str:
        v = self.vault_id_undashed
        if len(v) == 12:
            return f"{v[0:4]}-{v[4:8]}-{v[8:12]}"
        return "(no vault opened)"
