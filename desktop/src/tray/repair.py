"""Re-pair flow triggered from the auth-failure banner.

Wipes the appropriate scope locally (full vs pairing-only depending on
the latched ``AuthFailureKind``), re-registers a fresh keypair on the
``CREDENTIALS_INVALID`` path so the next pairing QR embeds a device id
the server actually knows, then launches the pairing subprocess.
"""

import logging

from ..connection import AuthFailureKind

log = logging.getLogger(__name__)


class RepairMixin:
    def _repair(self, *_) -> None:
        """User tapped the banner. Wipe the appropriate scope locally, then
        launch the pairing subprocess. ConnectionManager's flag is cleared so
        the banner disappears; the pairing flow restores creds.

        On full-wipe (401 Credentials Invalid), re-register with the newly
        generated keypair BEFORE the pairing subprocess starts — the pairing
        QR embeds the desktop's device_id, and the phone's sendPairingRequest
        will fail if that device_id doesn't exist in the server's devices
        table yet."""
        kind = self.conn.auth_failure_kind
        scope = "full" if kind == AuthFailureKind.CREDENTIALS_INVALID else "pairing_only"
        log.info("auth.repair.started scope=%s kind=%s", scope,
                 kind.value if kind else "none")
        try:
            self.poller.local_unpair(scope, notify_title="Re-pair started",
                                      notify_body="Follow the QR flow to reconnect.")
        except Exception:
            log.exception("local_unpair during repair failed")
        if scope == "full":
            if not self._reregister_after_wipe():
                # Let the user retry by leaving the latched flag visible.
                log.error("auth.repair.reregister.failed")
                try:
                    self.platform.notifications.notify(
                        "Re-pair failed",
                        "Couldn't re-register with the server. Check network and try again.",
                    )
                except Exception:
                    pass
                return
        self.conn.clear_auth_failure()
        self._update_icon()
        try:
            self._icon.update_menu()
        except Exception:
            pass
        # Launch the pairing subprocess (same path as "Pair..." menu entry).
        self._pair()

    def _reregister_after_wipe(self) -> bool:
        """Register the freshly generated keypair with the server and update
        the in-memory ConnectionManager. Returns True on success."""
        try:
            result = self.api.register(self.config.server_url)
        except Exception:
            log.exception("register call raised")
            return False
        if not result or "device_id" not in result or "auth_token" not in result:
            return False
        new_device_id = result["device_id"]
        new_auth_token = result["auth_token"]
        self.config.device_id = new_device_id
        self.config.auth_token = new_auth_token
        # ConnectionManager caches creds on construction — refresh under its
        # lock so an in-flight request on another thread can't observe a
        # half-updated (device_id, auth_token) pair.
        self.conn.update_credentials(new_device_id, new_auth_token)
        log.info("auth.repair.reregistered device_id=%s", new_device_id[:12])
        return True
