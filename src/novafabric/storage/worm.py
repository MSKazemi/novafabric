from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel


class WormReceipt(BaseModel):
    capsule_id: str
    backend_type: str          # "s3" | "azure" | "gcs" | "local"
    locked_until: AwareDatetime
    backend_confirmation_token: str   # S3 ETag, Azure versionId, GCS generation, or local row-id


class WormEntry(BaseModel):
    capsule_id: str
    locked_until: AwareDatetime
    size_bytes: int


class IntegrityResult(BaseModel):
    capsule_id: str
    ok: bool
    error: str = ""


@runtime_checkable
class WormAdapter(Protocol):
    def put(self, capsule_id: str, data: bytes, retention_days: int) -> WormReceipt: ...
    def get(self, capsule_id: str) -> bytes: ...
    def lock(self, capsule_id: str, hold_id: str) -> None: ...
    def list(self, prefix: str | None = None) -> list[WormEntry]: ...
    def verify_integrity(self, capsule_id: str) -> IntegrityResult: ...
