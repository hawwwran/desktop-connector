"""Best-effort op-log audit publishes for grant lifecycle events.

The three event types this module covers — ``vault.grant.created``,
``vault.revoke.completed``, ``vault.rotation.completed`` — are
server-side state changes (device-grant rows, access-secret slot)
that don't otherwise touch the encrypted manifest. To make these
show on every device's Activity timeline, the GTK4 caller hits this
helper after the corresponding grant op returns; it publishes one
fresh root revision carrying the matching entry.

The publish/retry shape is shared with the three other Phase 3.1
audit-publish call sites (``ops/clear.py``, ``ops/eviction.py``,
``migration/runner.py``) via
:func:`vault.state.op_log.publish_root_audit_entry`; this module
is a thin grant-flavoured wrapper that pins the dot-vocabulary
log prefix.
"""

from __future__ import annotations

from typing import Any

from ..state.op_log import publish_root_audit_entry


def publish_grant_lifecycle_audit(
    *,
    vault: Any,
    relay: Any,
    event_type: str,
    author_device_id: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Publish a fresh root revision carrying ``event_type`` on the tail.

    Returns ``True`` on success, ``False`` on any failure path (the
    caller can safely ignore the return; a WARN log line carries the
    event type and last error). Skips silently with ``False`` if
    ``author_device_id`` is empty so unattributed rows can't sneak
    onto the timeline.

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
    return publish_root_audit_entry(
        vault=vault, relay=relay,
        event_type=event_type,
        author_device_id=author_device_id,
        extra=extra,
        log_event_prefix="vault.grant.audit_publish",
    )


__all__ = ["publish_grant_lifecycle_audit"]
