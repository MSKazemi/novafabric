# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""AWS S3 / S3-compatible WORM adapter (Object Lock COMPLIANCE mode).

Requires: ``pip install novafabric[worm-s3]``  (boto3 >=1.35, Apache-2.0)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from novafabric.object_capsule_store.cas import sha256_to_base64
from novafabric.object_capsule_store.exceptions import CASMismatchError
from novafabric.object_capsule_store.worm.base import (
    ConditionalPutConflict,
    WormAdapter,
    WormPutResult,
)

log = logging.getLogger(__name__)


class S3WormAdapter(WormAdapter):
    """AWS S3 / S3-compatible WORM adapter using Object Lock COMPLIANCE mode.

    The bucket must have Object Lock enabled at creation time.
    """

    def __init__(
        self,
        bucket: str,
        client: Any | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        if client is not None:
            self._client: Any = client
        else:
            import boto3

            kwargs: dict[str, Any] = {}
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            self._client = boto3.client("s3", **kwargs)

    def put_object(
        self,
        key: str,
        data: bytes,
        sha256_hex: str,
        retention_days: int,
        content_type: str = "application/octet-stream",
    ) -> WormPutResult:
        locked_until = datetime.now(tz=timezone.utc) + timedelta(days=retention_days)
        checksum_b64 = sha256_to_base64(sha256_hex)
        try:
            resp = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                ChecksumSHA256=checksum_b64,
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=locked_until,
            )
        except Exception as exc:
            exc_str = str(exc)
            if (
                "InvalidChecksum" in exc_str
                or "BadDigest" in exc_str
                or "XAmzContentSHA256Mismatch" in exc_str
            ):
                raise CASMismatchError(
                    key=key, expected=sha256_hex, observed="<backend_rejected>"
                ) from exc
            raise
        return WormPutResult(
            key=key,
            confirmation_token=resp.get("ETag", ""),
            locked_until_ms=int(locked_until.timestamp() * 1000),
        )

    def apply_identical(
        self,
        key_a: str,
        data_a: bytes,
        sha256_a: str,
        key_b: str,
        data_b: bytes,
        sha256_b: str,
        retention_days: int,
    ) -> tuple[WormPutResult, WormPutResult]:
        result_a = self.put_object(key_a, data_a, sha256_a, retention_days)
        result_b = self.put_object(
            key_b, data_b, sha256_b, retention_days, content_type="application/json"
        )
        return result_a, result_b

    def get_object(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()  # type: ignore[no-any-return]

    def put_log_object(self, key: str, data: bytes) -> str:
        resp = self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType="application/json",
        )
        return str(resp.get("ETag", ""))

    def put_log_object_if_absent(self, key: str, data: bytes) -> str:
        try:
            resp = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType="application/json",
                IfNoneMatch="*",
            )
            return str(resp.get("ETag", ""))
        except Exception as exc:
            exc_str = str(exc)
            if (
                "PreconditionFailed" in exc_str
                or "412" in exc_str
                or "ConditionalRequestFailed" in exc_str
            ):
                raise ConditionalPutConflict(key) from exc
            raise

    def iter_objects(self, prefix: str) -> Iterator[str]:
        """Stream keys page-by-page via the S3 paginator (ADR-0175).

        boto3 pages at 1000 keys by default, so peak memory stays bounded even
        for namespaces with millions of objects — nothing is accumulated here.
        """
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def list_objects(self, prefix: str) -> list[str]:
        return sorted(self.iter_objects(prefix))

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False
