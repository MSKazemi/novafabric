"""S3-API abstraction using boto3 with configurable endpoint_url.

No minio SDK imports.  All writes go via boto3 with endpoint_url from config.
cap-009 / ADR-0066.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class ObjectLockNotSupportedError(Exception):
    """Raised when the S3 backend does not support Object Lock COMPLIANCE mode."""


class NovaObjectStore:
    """S3-compatible object store abstraction.

    Works with AWS S3, MinIO, Ceph RGW, and any S3-compatible backend.
    Configure via NOVA_S3_ENDPOINT_URL, NOVA_S3_BUCKET, NOVA_S3_ACCESS_KEY,
    NOVA_S3_SECRET_KEY.  cap-009 / ADR-0066.
    """

    def __init__(
        self,
        endpoint_url: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url or os.getenv("NOVA_S3_ENDPOINT_URL")
        self.bucket = bucket or os.getenv("NOVA_S3_BUCKET", "nova-capsules")
        self.access_key = access_key or os.getenv("NOVA_S3_ACCESS_KEY")
        self.secret_key = secret_key or os.getenv("NOVA_S3_SECRET_KEY")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "boto3 required for NovaObjectStore. "
                    "Install with: pip install novafabric[scale]"
                ) from exc

            kwargs: dict[str, Any] = {"service_name": "s3"}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            if self.access_key and self.secret_key:
                kwargs["aws_access_key_id"] = self.access_key
                kwargs["aws_secret_access_key"] = self.secret_key
            self._client = boto3.client(**kwargs)
        return self._client

    def validate(self) -> dict[str, Any]:
        """Verify the bucket supports Object Lock COMPLIANCE mode.

        Returns info dict on success; raises ObjectLockNotSupportedError otherwise.
        """
        client = self._get_client()
        try:
            resp = client.get_bucket_object_lock_configuration(Bucket=self.bucket)
            enabled = (
                resp.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled")
                == "Enabled"
            )
            if not enabled:
                raise ObjectLockNotSupportedError(
                    f"Bucket {self.bucket!r} does not have Object Lock enabled"
                )
            return {
                "bucket": self.bucket,
                "object_lock": "COMPLIANCE",
                "endpoint": self.endpoint_url,
            }
        except ObjectLockNotSupportedError:
            raise
        except Exception as exc:
            # Surface boto3 error about missing config as our domain error.
            exc_name = type(exc).__name__
            is_no_lock = (
                "ObjectLockConfigurationNotFoundError" in exc_name
                or "NoSuchObjectLockConfiguration" in exc_name
            )
            if is_no_lock:
                raise ObjectLockNotSupportedError(
                    f"Bucket {self.bucket!r} has no Object Lock configuration"
                ) from exc
            raise

    def put_object(
        self,
        key: str,
        data: bytes,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Put an object; returns ETag string."""
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
        }
        if metadata:
            kwargs["Metadata"] = {k: str(v) for k, v in metadata.items()}
        resp = client.put_object(**kwargs)
        return str(resp.get("ETag", ""))

    def delete_object(self, key: str) -> None:
        """Delete a PII payload object (GOVERNANCE mode allows deletion)."""
        client = self._get_client()
        client.delete_object(Bucket=self.bucket, Key=key)

    def get_object_metadata(self, key: str) -> dict[str, Any]:
        """Get object metadata without downloading content."""
        client = self._get_client()
        resp = client.head_object(Bucket=self.bucket, Key=key)
        return {
            "key": key,
            "size": resp.get("ContentLength"),
            "etag": resp.get("ETag"),
        }
