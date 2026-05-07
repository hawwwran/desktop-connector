"""Typed payloads returned from chunk upload / download / registration."""

from dataclasses import dataclass


@dataclass
class ChunkUploadOutcome:
    status: str
    body: dict | None = None
    abort_reason: str | None = None
    http_status: int | None = None


@dataclass
class ChunkDownloadOutcome:
    status: str
    data: bytes | None = None
    retry_after_ms: int | None = None
    abort_reason: str | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class DeviceRegistrationResult:
    status_code: int
    body: dict | None = None

    @property
    def is_successful(self) -> bool:
        return 200 <= self.status_code < 300 and self.body is not None
