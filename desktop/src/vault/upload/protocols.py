"""Structural typing for the upload paths' vault + relay collaborators.

Production wires these to ``vault.Vault`` and ``api_client.ApiClient``
(or a thin adapter); tests pass in fakes that match the same shape.
"""

from typing import Any, Protocol


class UploadVault(Protocol):
    @property
    def vault_id(self) -> str: ...

    @property
    def master_key(self) -> bytes | None: ...

    @property
    def vault_access_secret(self) -> str | None: ...

    def fetch_manifest(self, relay, *, local_index=None) -> dict: ...

    def publish_manifest(self, relay, manifest, *, local_index=None) -> dict: ...


class UploadRelay(Protocol):
    def batch_head_chunks(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]: ...

    def put_chunk(
        self,
        vault_id: str,
        vault_access_secret: str,
        chunk_id: str,
        body: bytes,
    ) -> dict[str, Any]: ...

    def put_manifest(self, *args, **kwargs) -> Any: ...
