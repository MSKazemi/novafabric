"""Destination resolution + S3 path via the existing adapter surface (ADR-0141 D1/P2)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from export_blob.helpers import make_capsule
from novafabric.export_blob.destinations import (
    DestinationError,
    LocalDirDestination,
    S3Destination,
    blob_key,
    resolve_destination,
    worm_retain_until,
)
from novafabric.export_blob.models import MANIFEST_FILENAME, WormIntent
from novafabric.export_blob.service import (
    CapsuleSelection,
    VerifyStatus,
    export_batch,
    verify_export_manifest,
)


class _NotFound(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "404"}}


class FakeS3Client:
    """Minimal S3 client double covering the calls the destination makes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_kwargs: dict[str, dict[str, object]] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: object) -> dict[str, str]:
        assert Bucket == "audit-bucket"
        self.objects[Key] = bytes(Body)
        self.put_kwargs[Key] = kwargs
        return {"ETag": "fake-etag"}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            raise _NotFound()
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            raise _NotFound()
        return {"Body": io.BytesIO(self.objects[Key])}


class TestResolveDestination:
    def test_local_path(self, tmp_path: Path) -> None:
        dest = resolve_destination(str(tmp_path))
        assert isinstance(dest, LocalDirDestination)
        assert dest.uri == str(tmp_path)

    def test_s3_uri_parses_bucket_and_prefix(self) -> None:
        client = FakeS3Client()
        dest = resolve_destination("s3://audit-bucket/exports/2026-07", s3_client=client)
        assert isinstance(dest, S3Destination)
        dest.put_blob("sha256:" + "0" * 64, b"x")
        assert "exports/2026-07/objects/" + "0" * 64 in client.objects

    def test_s3_uri_without_bucket_rejected(self) -> None:
        with pytest.raises(DestinationError, match="no bucket"):
            resolve_destination("s3://")

    @pytest.mark.parametrize("scheme", ["azure", "gcs"])
    def test_azure_gcs_planned_not_implemented(self, scheme: str) -> None:
        with pytest.raises(DestinationError, match="planned"):
            resolve_destination(f"{scheme}://container/path")


class TestS3Destination:
    def _dest(self, client: FakeS3Client) -> S3Destination:
        return S3Destination(
            "audit-bucket",
            "exports/",
            "s3://audit-bucket/exports/",
            client=client,
            worm_retention_days=7,
        )

    def test_export_and_verify_round_trip_via_s3(
        self, capsule_root: Path, tmp_path: Path, signer, public_pem: bytes
    ) -> None:
        client = FakeS3Client()
        dest = self._dest(client)
        capsules = [make_capsule(capsule_root, "run-a")]
        selection = CapsuleSelection(capsules, query=None, query_resolved_at="2026-07-14T00:00:00Z")
        result = export_batch(selection, dest, signer)

        manifest_key = "exports/" + MANIFEST_FILENAME
        assert manifest_key in client.objects
        member = result.manifest.members[0]
        assert "exports/" + blob_key(member.content_hash) in client.objects

        # verify offline against the same (fake) endpoint
        local_manifest = tmp_path / MANIFEST_FILENAME
        local_manifest.write_bytes(client.objects[manifest_key])
        report = verify_export_manifest(
            local_manifest, public_pem, s3_client=client
        )
        assert report.status is VerifyStatus.VALID

    def test_idempotent_skip_uses_head(self, capsule_root: Path, signer) -> None:
        client = FakeS3Client()
        capsules = [make_capsule(capsule_root, "run-a")]
        selection = CapsuleSelection(capsules, query=None, query_resolved_at="2026-07-14T00:00:00Z")
        first = export_batch(selection, self._dest(client), signer)
        second = export_batch(selection, self._dest(client), signer)
        assert first.written == 1
        assert second.written == 0 and second.skipped == 1

    def test_worm_put_goes_through_object_lock_adapter(
        self, capsule_root: Path, signer
    ) -> None:
        client = FakeS3Client()
        dest = self._dest(client)
        capsules = [make_capsule(capsule_root, "run-a")]
        selection = CapsuleSelection(capsules, query=None, query_resolved_at="2026-07-14T00:00:00Z")
        worm = WormIntent(mode="compliance", retain_until=worm_retain_until(7))
        result = export_batch(selection, dest, signer, worm=worm)

        blob_kwargs = client.put_kwargs["exports/" + blob_key(result.manifest.members[0].content_hash)]
        manifest_kwargs = client.put_kwargs["exports/" + MANIFEST_FILENAME]
        for kwargs in (blob_kwargs, manifest_kwargs):
            assert kwargs["ObjectLockMode"] == "COMPLIANCE"
            assert "ObjectLockRetainUntilDate" in kwargs

    def test_missing_blob_returns_none(self) -> None:
        dest = self._dest(FakeS3Client())
        assert dest.get_blob("sha256:" + "0" * 64) is None

    def test_head_reraises_non_404(self) -> None:
        class BrokenClient(FakeS3Client):
            def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
                raise RuntimeError("access denied")

        dest = self._dest(BrokenClient())
        with pytest.raises(RuntimeError, match="access denied"):
            dest.blob_exists("sha256:" + "0" * 64)


def test_worm_retain_until_is_rfc3339_utc() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    assert worm_retain_until(365, now=now) == "2027-07-12T00:00:00.000000Z"
