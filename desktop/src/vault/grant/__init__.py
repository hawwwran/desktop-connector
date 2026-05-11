"""Per-device grant material — local unlock secrets + cross-device join flow.

Submodules:
- ``store`` — ``VaultGrant`` + keyring/fallback ``GrantStore`` backends;
  on-disk shape at ``<config_dir>/vault_grant_<vault_id>.json``
- ``qr`` — pairing-QR encode/decode for cross-device grants
- ``wrap`` — recovery-kit wrap/unwrap primitives (passphrase → wrap key)
- ``access_rotation`` — vault-access-token rotation (T15 spec)
"""
