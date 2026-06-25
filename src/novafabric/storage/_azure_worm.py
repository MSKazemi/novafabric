from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .worm import IntegrityResult, WormEntry, WormReceipt


class AzureWormAdapter:
    """Azure Blob Storage immutable blob WORM adapter.

    Requires the container to have an immutability policy configured.
    azure-storage-blob must be installed: pip install novafabric[worm-azure]
    """

    def __init__(self, container_client: Any) -> None:
        # container_client: azure.storage.blob.ContainerClient
        self._container = container_client

    def put(self, capsule_id: str, data: bytes, retention_days: int) -> WormReceipt:
        locked_until = datetime.now(tz=timezone.utc) + timedelta(days=retention_days)
        blob_client = self._container.get_blob_client(capsule_id)
        resp = blob_client.upload_blob(data, overwrite=False)
        return WormReceipt(
            capsule_id=capsule_id,
            backend_type="azure",
            locked_until=locked_until,
            backend_confirmation_token=resp.get("version_id", resp.get("etag", "")),
        )

    def get(self, capsule_id: str) -> bytes:
        blob_client = self._container.get_blob_client(capsule_id)
        return blob_client.download_blob().readall()  # type: ignore[no-any-return]

    def lock(self, capsule_id: str, hold_id: str) -> None:
        blob_client = self._container.get_blob_client(capsule_id)
        blob_client.set_legal_hold(True)

    def list(self, prefix: str | None = None) -> list[WormEntry]:
        blobs = self._container.list_blobs(name_starts_with=prefix)
        return [
            WormEntry(
                capsule_id=b.name,
                # placeholder — Azure immutability is a container-level policy;
                # per-blob locked_until is not returned by list_blobs.
                locked_until=datetime.now(tz=timezone.utc) + timedelta(days=1),
                size_bytes=b.size,
            )
            for b in blobs
        ]

    def verify_integrity(self, capsule_id: str) -> IntegrityResult:
        try:
            blob_client = self._container.get_blob_client(capsule_id)
            blob_client.get_blob_properties()
            return IntegrityResult(capsule_id=capsule_id, ok=True)
        except Exception as exc:
            return IntegrityResult(capsule_id=capsule_id, ok=False, error=str(exc))
