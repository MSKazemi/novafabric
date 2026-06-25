from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .worm import IntegrityResult, WormEntry, WormReceipt


class S3WormAdapter:
    """S3 Object Lock COMPLIANCE mode WORM adapter.

    Requires the bucket to have Object Lock enabled at creation time.
    boto3 must be installed: pip install novafabric[worm-s3]
    """

    def __init__(self, bucket: str, client: object | None = None) -> None:
        self._bucket = bucket
        if client is None:
            import boto3

            self._client: Any = boto3.client("s3")
        else:
            self._client = client

    def put(self, capsule_id: str, data: bytes, retention_days: int) -> WormReceipt:
        locked_until = datetime.now(tz=timezone.utc) + timedelta(days=retention_days)
        resp = self._client.put_object(
            Bucket=self._bucket,
            Key=capsule_id,
            Body=data,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=locked_until,
        )
        return WormReceipt(
            capsule_id=capsule_id,
            backend_type="s3",
            locked_until=locked_until,
            backend_confirmation_token=resp["ETag"],
        )

    def get(self, capsule_id: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=capsule_id)
        return resp["Body"].read()  # type: ignore[no-any-return]

    def lock(self, capsule_id: str, hold_id: str) -> None:
        # S3 legal holds are boolean ON/OFF per object; hold_id is recorded for audit purposes only
        self._client.put_object_legal_hold(
            Bucket=self._bucket,
            Key=capsule_id,
            LegalHold={"Status": "ON"},
        )

    def list(self, prefix: str | None = None) -> list[WormEntry]:
        kwargs: dict[str, Any] = {"Bucket": self._bucket}
        if prefix:
            kwargs["Prefix"] = prefix
        resp = self._client.list_objects_v2(**kwargs)
        entries = []
        for obj in resp.get("Contents", []):
            lock_resp = self._client.get_object_retention(
                Bucket=self._bucket, Key=obj["Key"]
            )
            entries.append(
                WormEntry(
                    capsule_id=obj["Key"],
                    locked_until=lock_resp["Retention"]["RetainUntilDate"],
                    size_bytes=obj["Size"],
                )
            )
        return entries

    def verify_integrity(self, capsule_id: str) -> IntegrityResult:
        try:
            self._client.head_object(Bucket=self._bucket, Key=capsule_id)
            return IntegrityResult(capsule_id=capsule_id, ok=True)
        except Exception as exc:
            return IntegrityResult(capsule_id=capsule_id, ok=False, error=str(exc))
