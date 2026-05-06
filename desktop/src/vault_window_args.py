"""F-U14 — vault id resolution for subprocess GTK windows.

The dispatcher (`src.windows`) accepts an optional ``--vault-id`` arg
and threads the normalized form into every ``show_vault_*`` entry
point. Today every vault window resolves the active id off
``config['vault']['last_known_id']``; with this hook, a future
multi-vault tray (or a smoke-test driver) can repoint a subprocess at
a specific vault without having to rewrite config on disk between
launches.

Two pure helpers:

- :func:`parse_vault_id_arg` validates and normalizes the CLI string.
  Raises :class:`ValueError` on malformed input so the dispatcher can
  surface a clean ``argparse`` error instead of letting the window
  open silently against the wrong vault.

- :func:`resolve_active_vault_id` is the small router each window's
  ``local_vault_id()`` closure delegates to: explicit override wins,
  fall back to a fresh ``config.reload()`` read of ``last_known_id``
  for backwards compatibility with the tray's current single-vault
  wiring.

Both are GTK-free so the protocol tests can import them without the
host needing libadwaita. They live alongside the GTK windows in
``desktop/src/`` because they're a hop on the dispatcher → window
path; no other module depends on them.
"""

from __future__ import annotations


_BASE32_UPPER_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def parse_vault_id_arg(raw: str | None) -> str | None:
    """Normalize a ``--vault-id`` CLI argument into the canonical 12-char form.

    Accepts ``ABCD2345WXYZ`` or ``ABCD-2345-WXYZ`` (case-insensitive).
    Returns ``None`` for ``None`` / empty so callers keep the
    ``last_known_id`` fallback. Raises :class:`ValueError` for malformed
    input — the dispatcher converts that into an argparse error so the
    subprocess fails fast instead of silently routing to the wrong vault.

    Validation: strict RFC 4648 base32 alphabet (A–Z, 2–7) + 12-char
    length; matches :func:`vault._generate_vault_id`'s output and the
    AAD encoding (formats §6.1).
    """
    if raw is None:
        return None
    candidate = raw.strip().replace("-", "").upper()
    if not candidate:
        return None
    if len(candidate) != 12:
        raise ValueError(
            f"vault id must be 12 base32 characters, got {len(candidate)}",
        )
    bad = [c for c in candidate if c not in _BASE32_UPPER_ALPHABET]
    if bad:
        raise ValueError(
            f"vault id contains non-base32 characters: "
            f"{''.join(sorted(set(bad)))}",
        )
    return candidate


def resolve_active_vault_id(config, vault_id_override: str | None) -> str:
    """Pick the active vault id for a subprocess window.

    Returns the explicit ``--vault-id`` override when present (already
    normalized by :func:`parse_vault_id_arg`); otherwise reads
    ``config['vault']['last_known_id']`` after a fresh ``config.reload()``
    so the wizard's writes show up across subprocess boundaries.

    Returns an empty string when neither source has a usable id, matching
    the legacy "no vault opened" placeholder behaviour.
    """
    if vault_id_override:
        return vault_id_override
    config.reload()
    raw = config._data.get("vault")
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("last_known_id") or "")
