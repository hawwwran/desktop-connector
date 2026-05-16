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
        initial_root_ciphertext: bytes,
        initial_root_hash: str,
    ) -> dict: ...

    def get_header(
        self,
        vault_id: str,
        vault_access_secret: str,
    ) -> dict: ...

    # Legacy unified-manifest surface — kept on the Protocol during
    # Phase D so test fakes can keep their pre-sharding implementations
    # while production code migrates to ``get_root``/``get_shard``.
    # Phase H removes both.
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

    def get_root(
        self,
        vault_id: str,
        vault_access_secret: str,
    ) -> dict: ...

    def put_root(
        self,
        vault_id: str,
        vault_access_secret: str,
        *,
        expected_current_root_revision: int,
        new_root_revision: int,
        parent_root_revision: int,
        root_hash: str,
        root_ciphertext: bytes,
    ) -> dict: ...

    def get_shard(
        self,
        vault_id: str,
        vault_access_secret: str,
        remote_folder_id: str,
    ) -> dict: ...

    def put_shard(
        self,
        vault_id: str,
        vault_access_secret: str,
        remote_folder_id: str,
        *,
        expected_current_shard_revision: int,
        new_shard_revision: int,
        parent_shard_revision: int,
        shard_hash: str,
        shard_ciphertext: bytes,
    ) -> dict: ...

    def put_shard_with_root(
        self,
        vault_id: str,
        vault_access_secret: str,
        remote_folder_id: str,
        *,
        shard: dict,
        root: dict,
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
