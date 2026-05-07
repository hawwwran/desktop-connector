"""Subset of the relay API surface that the Vault class needs.

Production wires this to ``api_client.ApiClient`` (or a thin wrapper
around it); tests pass a fake. Methods take primitive types so the
protocol stays agnostic to HTTP transport.
"""

from typing import Protocol


class RelayProtocol(Protocol):
    def create_vault(
        self,
        vault_id: str,
        vault_access_token_hash: bytes,
        encrypted_header: bytes,
        header_hash: str,
        initial_manifest_ciphertext: bytes,
        initial_manifest_hash: str,
    ) -> dict: ...

    def get_header(
        self,
        vault_id: str,
        vault_access_secret: str,
    ) -> dict: ...

    def get_manifest(
        self,
        vault_id: str,
        vault_access_secret: str,
    ) -> dict: ...

    def put_manifest(
        self,
        vault_id: str,
        vault_access_secret: str,
        *,
        expected_current_revision: int,
        new_revision: int,
        parent_revision: int,
        manifest_hash: str,
        manifest_ciphertext: bytes,
    ) -> dict: ...

    def batch_head_chunks(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_ids: list[str],
    ) -> dict: ...

    def get_chunk(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_id: str,
    ) -> bytes: ...
