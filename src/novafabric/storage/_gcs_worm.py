from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .worm import IntegrityResult, WormEntry, WormReceipt


class GCSWormAdapter:
    """Google Cloud Storage Bucket Lock WORM adapter.

    Requires the bucket to have a retention policy (retentionPolicy) configured.
    google-cloud-storage must be installed: pip install novafabric[worm-gcs]
    """

    def __init__(self, bucket: Any) -> None:
        # bucket: google.cloud.storage.Bucket
        self._bucket = bucket

    def put(self, capsule_id: str, data: bytes, retention_days: int) -> WormReceipt:
        locked_until = datetime.now(tz=timezone.utc) + timedelta(days=retention_days)
        blob = self._bucket.blob(capsule_id)
        blob.upload_from_string(data)
        return WormReceipt(
            capsule_id=capsule_id,
            backend_type="gcs",
            locked_until=locked_until,
            backend_confirmation_token=str(blob.generation or ""),
        )

    def get(self, capsule_id: str) -> bytes:
        blob = self._bucket.blob(capsule_id)
        return blob.download_as_bytes()  # type: ignore[no-any-return]

    def lock(self, capsule_id: str, hold_id: str) -> None:
        blob = self._bucket.blob(capsule_id)
        blob.temporary_hold = True
        blob.patch()

    def list(self, prefix: str | None = None) -> list[WormEntry]:
        blobs = list(self._bucket.list_blobs(prefix=prefix))
        return [
            WormEntry(
                capsule_id=b.name,
                # placeholder — GCS retention is a bucket-level policy;
                # per-blob locked_until is not returned by list_blobs.
                locked_until=datetime.now(tz=timezone.utc) + timedelta(days=1),
                size_bytes=b.size or 0,
            )
            for b in blobs
        ]

    def verify_integrity(self, capsule_id: str) -> IntegrityResult:
        try:
            blob = self._bucket.blob(capsule_id)
            if not blob.exists():
                return IntegrityResult(capsule_id=capsule_id, ok=False, error="not found")
            return IntegrityResult(capsule_id=capsule_id, ok=True)
        except Exception as exc:
            return IntegrityResult(capsule_id=capsule_id, ok=False, error=str(exc))
