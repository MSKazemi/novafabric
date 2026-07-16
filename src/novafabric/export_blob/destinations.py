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

"""Export destinations over the existing storage adapters (ADR-0141 D1).

A destination stores content-addressed member blobs under ``objects/<sha256hex>``
plus the ``export-manifest.json`` completion marker. Two destinations ship:

- ``LocalDirDestination`` — a plain directory; always works, fully offline.
- ``S3Destination`` — any S3-compatible endpoint via the **existing** optional
  boto3 path; WORM writes are delegated to the shipped
  :class:`novafabric.storage._s3_worm.S3WormAdapter` (ADR-0031/0062).

``azure://`` / ``gcs://`` are planned (ADR-0141 P2) and raise a clear error.
No new storage backend is introduced.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

_OBJECTS_PREFIX = "objects/"


class DestinationError(Exception):
    """The destination URI cannot be resolved or written."""


def blob_key(content_hash: str) -> str:
    """Relative CAS key for a member blob: ``objects/<sha256hex>``."""
    return _OBJECTS_PREFIX + content_hash.removeprefix("sha256:")


class ExportDestination(Protocol):
    """Where member blobs and the manifest are written / read back."""

    uri: str

    def blob_exists(self, content_hash: str) -> bool:
        """True if the content-addressed blob is already present (skip → resume)."""
        ...

    def put_blob(self, content_hash: str, data: bytes, *, worm: bool = False) -> None:
        """Write one member blob content-addressed; optionally under WORM retention."""
        ...

    def get_blob(self, content_hash: str) -> bytes | None:
        """Read back a member blob for verification; ``None`` if missing."""
        ...

    def put_manifest(self, name: str, data: bytes, *, worm: bool = False) -> None:
        """Write the manifest object — always the final write of an export."""
        ...


class LocalDirDestination:
    """A local directory destination (local-first default; no service, no cloud)."""

    def __init__(self, root: Path, uri: str | None = None) -> None:
        self.root = root
        self.uri = uri if uri is not None else str(root)

    def _blob_path(self, content_hash: str) -> Path:
        return self.root / blob_key(content_hash)

    def blob_exists(self, content_hash: str) -> bool:
        return self._blob_path(content_hash).is_file()

    def put_blob(self, content_hash: str, data: bytes, *, worm: bool = False) -> None:
        self._atomic_write(self._blob_path(content_hash), data, worm=worm)

    def get_blob(self, content_hash: str) -> bytes | None:
        path = self._blob_path(content_hash)
        if not path.is_file():
            return None
        return path.read_bytes()

    def put_manifest(self, name: str, data: bytes, *, worm: bool = False) -> None:
        self._atomic_write(self.root / name, data, worm=worm)

    @staticmethod
    def _atomic_write(path: Path, data: bytes, *, worm: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        if worm:
            # Local best-effort immutability; real retention is destination-side
            # (ADR-0031 caveat — the manifest records the *intent*).
            tmp.chmod(0o444)
        if path.exists():
            path.unlink()
        tmp.rename(path)


class S3Destination:
    """Any S3-compatible endpoint via the existing optional boto3 path.

    WORM puts delegate to the shipped ``S3WormAdapter`` (Object Lock COMPLIANCE
    mode, ADR-0031); plain puts use the same client. The endpoint is always
    configurable (``endpoint_url`` / ``NOVA_S3_ENDPOINT_URL``) so any private
    S3-compatible OSS deployment works — no hosted service is required.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str,
        uri: str,
        client: object | None = None,
        endpoint_url: str | None = None,
        worm_retention_days: int = 365,
    ) -> None:
        self.uri = uri
        self._bucket = bucket
        self._prefix = prefix
        self._retention_days = worm_retention_days
        if client is None:  # pragma: no cover — exercised only with boto3 installed
            import os

            try:
                import boto3
            except ImportError as exc:
                raise DestinationError(
                    "boto3 is required for s3:// destinations: "
                    'pip install "novafabric[worm-s3]"'
                ) from exc
            endpoint = endpoint_url or os.environ.get("NOVA_S3_ENDPOINT_URL")
            client = boto3.client("s3", endpoint_url=endpoint)
        self._client: Any = client
        # Reuse the shipped WORM adapter for retention writes (ADR-0031/0062).
        from novafabric.storage._s3_worm import S3WormAdapter

        self._worm_adapter = S3WormAdapter(bucket, client=self._client)

    def _key(self, relative: str) -> str:
        return self._prefix + relative

    def blob_exists(self, content_hash: str) -> bool:
        return self._head(self._key(blob_key(content_hash)))

    def _head(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception as exc:
            if _is_not_found(exc):
                return False
            raise

    def put_blob(self, content_hash: str, data: bytes, *, worm: bool = False) -> None:
        self._put(self._key(blob_key(content_hash)), data, worm=worm)

    def get_blob(self, content_hash: str) -> bytes | None:
        try:
            resp = self._client.get_object(
                Bucket=self._bucket, Key=self._key(blob_key(content_hash))
            )
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        return bytes(resp["Body"].read())

    def put_manifest(self, name: str, data: bytes, *, worm: bool = False) -> None:
        self._put(self._key(name), data, worm=worm)

    def _put(self, key: str, data: bytes, *, worm: bool) -> None:
        if worm:
            self._worm_adapter.put(key, data, retention_days=self._retention_days)
        else:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data)


def _is_not_found(exc: Exception) -> bool:
    """True for boto3 ClientError shapes that mean 'object does not exist'."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    return code in ("404", "NoSuchKey", "NotFound")


def resolve_destination(
    dest: str,
    *,
    s3_client: object | None = None,
    s3_endpoint_url: str | None = None,
    worm_retention_days: int = 365,
) -> ExportDestination:
    """Resolve a ``--dest`` URI to a destination over an existing adapter.

    ``s3://bucket[/prefix]`` → :class:`S3Destination`; anything else that is not
    a known-but-unimplemented cloud scheme is a local directory path.
    """
    if dest.startswith("s3://"):
        rest = dest[len("s3://") :]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise DestinationError(f"invalid s3:// destination (no bucket): {dest!r}")
        prefix = prefix.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return S3Destination(
            bucket,
            prefix,
            dest,
            client=s3_client,
            endpoint_url=s3_endpoint_url,
            worm_retention_days=worm_retention_days,
        )
    if dest.startswith(("azure://", "gcs://")):
        raise DestinationError(
            f"{dest.split('://', 1)[0]}:// destinations are planned (ADR-0141 P2) "
            "and not implemented in this release — use s3:// or a local directory."
        )
    return LocalDirDestination(Path(dest), uri=dest)


def worm_retain_until(retention_days: int, *, now: datetime | None = None) -> str:
    """RFC 3339 ``retain_until`` for a retention window starting *now*."""
    base = now if now is not None else datetime.now(tz=timezone.utc)
    retained = base.timestamp() + retention_days * 86400
    stamp = datetime.fromtimestamp(retained, tz=timezone.utc)
    return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")
