"""Best-effort op-log audit publishes for grant lifecycle events.

The three event types this module covers — ``vault.grant.created``,
``vault.revoke.completed``, ``vault.rotation.completed`` — are
server-side state changes (device-grant rows, access-secret slot)
that don't otherwise touch the encrypted manifest. To make these
show on every device's Activity timeline, the GTK4 caller hits this
helper after the corresponding grant op returns; it publishes one
fresh root revision carrying the matching entry.

Best-effort by design: the grant op has already committed
relay-side, so a publish failure here is cosmetic. CAS-retried a
small number of times to absorb a concurrent root mutation from
another device.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from ..relay_errors import VaultCASConflictError
from ..state.op_log import (
    append_op_log_entries,
    build_op_log_entry,
    maybe_genesis_followup_entries,
)


log = logging.getLogger(__name__)


_GRANT_AUDIT_PUBLISH_RETRIES = 3


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def publish_grant_lifecycle_audit(
    *,
    vault: Any,
    relay: Any,
    event_type: str,
    author_device_id: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Publish a fresh root revision carrying ``event_type`` on the tail.

    Returns ``True`` on success, ``False`` on any failure path
    (no-op caller can ignore the return; a debug breadcrumb is
    logged on miss). Skips silently if ``author_device_id`` is empty
    so unattributed rows can't sneak onto the timeline.

    ``event_type`` should be one of:

    - ``"vault.grant.created"`` — recommended extras:
      ``approved_role``, ``claimant_device_id``.
    - ``"vault.revoke.completed"`` — recommended extras:
      ``target_device_id``, ``revoked_at``, ``already_revoked``.
    - ``"vault.rotation.completed"`` — recommended extras:
      ``rotated_at``.

    The encrypted manifest's tail is the only audit surface (the
    server-side ``vault_audit_events`` table was retired in F-S16),
    so a missed row here is genuinely missed — not duplicated
    elsewhere. The corresponding caller-side ``log.info`` lines
    remain as the local breadcrumb regardless of publish outcome.
    """
    if not author_device_id:
        return False
    last_exc: Exception | None = None
    for attempt in range(_GRANT_AUDIT_PUBLISH_RETRIES):
        try:
            current_root = vault.fetch_root_manifest(relay)
            parent_revision = int(current_root.get("root_revision", 0))
            new_revision = parent_revision + 1
            timestamp = _now_rfc3339()
            candidate = copy.deepcopy(current_root)
            candidate["root_revision"] = new_revision
            candidate["parent_root_revision"] = parent_revision
            candidate["created_at"] = timestamp
            candidate["author_device_id"] = str(author_device_id)
            create_entries = maybe_genesis_followup_entries(
                current_root,
                new_revision=new_revision,
                device_id=author_device_id,
            )
            candidate["operation_log_tail"] = append_op_log_entries(
                candidate.get("operation_log_tail"),
                [
                    *create_entries,
                    build_op_log_entry(
                        event_type=event_type,
                        device_id=author_device_id,
                        revision=new_revision,
                        extra=extra,
                    ),
                ],
            )
            vault.publish_root_manifest(relay, candidate)
            return True
        except VaultCASConflictError as exc:
            last_exc = exc
            log.info(
                "vault.grant.audit_publish.cas_retry "
                "event=%s attempt=%d/%d",
                event_type, attempt + 1, _GRANT_AUDIT_PUBLISH_RETRIES,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            break
    log.warning(
        "vault.grant.audit_publish.failed event=%s last_error=%r",
        event_type, last_exc,
    )
    return False


__all__ = ["publish_grant_lifecycle_audit"]
