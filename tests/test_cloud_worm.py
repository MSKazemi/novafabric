"""Tests for cloud WORM adapters (S3, Azure, GCS) — all mocked, no live cloud."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from novafabric.storage._azure_worm import AzureWormAdapter
from novafabric.storage._gcs_worm import GCSWormAdapter
from novafabric.storage._s3_worm import S3WormAdapter
from novafabric.storage.worm import WormAdapter

# ---------------------------------------------------------------------------
# S3 adapter
# ---------------------------------------------------------------------------

def test_s3_put_sets_object_lock() -> None:
    client = MagicMock()
    client.put_object.return_value = {"ETag": '"abc123"'}
    adapter = S3WormAdapter(bucket="test-bucket", client=client)
    receipt = adapter.put("cap1", b"data", retention_days=30)
    assert receipt.backend_type == "s3"
    assert receipt.backend_confirmation_token == '"abc123"'
    call_kwargs = client.put_object.call_args.kwargs
    assert call_kwargs["ObjectLockMode"] == "COMPLIANCE"
    assert call_kwargs["Bucket"] == "test-bucket"
    assert call_kwargs["Key"] == "cap1"


def test_s3_lock_sets_legal_hold() -> None:
    client = MagicMock()
    adapter = S3WormAdapter(bucket="test-bucket", client=client)
    adapter.lock("cap1", "hold-001")
    client.put_object_legal_hold.assert_called_once()
    assert client.put_object_legal_hold.call_args.kwargs["LegalHold"]["Status"] == "ON"


def test_s3_get_returns_body() -> None:
    client = MagicMock()
    client.get_object.return_value = {"Body": MagicMock(read=lambda: b"data")}
    adapter = S3WormAdapter(bucket="test-bucket", client=client)
    assert adapter.get("cap1") == b"data"


def test_s3_verify_integrity_ok() -> None:
    client = MagicMock()
    adapter = S3WormAdapter(bucket="b", client=client)
    result = adapter.verify_integrity("cap1")
    assert result.ok


def test_s3_verify_integrity_fail() -> None:
    client = MagicMock()
    client.head_object.side_effect = Exception("NoSuchKey")
    adapter = S3WormAdapter(bucket="b", client=client)
    result = adapter.verify_integrity("cap1")
    assert not result.ok
    assert "NoSuchKey" in result.error


def test_s3_list_returns_entries() -> None:
    client = MagicMock()
    locked_until = datetime.now(tz=timezone.utc) + timedelta(days=30)
    client.list_objects_v2.return_value = {
        "Contents": [{"Key": "cap1", "Size": 42}]
    }
    client.get_object_retention.return_value = {
        "Retention": {"RetainUntilDate": locked_until}
    }
    adapter = S3WormAdapter(bucket="b", client=client)
    entries = adapter.list()
    assert len(entries) == 1
    assert entries[0].capsule_id == "cap1"
    assert entries[0].size_bytes == 42


def test_s3_implements_protocol() -> None:
    client = MagicMock()
    adapter = S3WormAdapter(bucket="b", client=client)
    assert isinstance(adapter, WormAdapter)


# ---------------------------------------------------------------------------
# Azure adapter
# ---------------------------------------------------------------------------

def test_azure_put_uploads_blob() -> None:
    container = MagicMock()
    blob_client = MagicMock()
    blob_client.upload_blob.return_value = {"version_id": "ver1", "etag": "etag1"}
    container.get_blob_client.return_value = blob_client
    adapter = AzureWormAdapter(container_client=container)
    receipt = adapter.put("cap1", b"data", retention_days=30)
    assert receipt.backend_type == "azure"
    blob_client.upload_blob.assert_called_once_with(b"data", overwrite=False)


def test_azure_put_token_falls_back_to_etag() -> None:
    container = MagicMock()
    blob_client = MagicMock()
    blob_client.upload_blob.return_value = {"etag": "etag-only"}
    container.get_blob_client.return_value = blob_client
    adapter = AzureWormAdapter(container_client=container)
    receipt = adapter.put("cap1", b"data", retention_days=7)
    assert receipt.backend_confirmation_token == "etag-only"


def test_azure_lock_sets_legal_hold() -> None:
    container = MagicMock()
    blob_client = MagicMock()
    container.get_blob_client.return_value = blob_client
    adapter = AzureWormAdapter(container_client=container)
    adapter.lock("cap1", "hold-001")
    blob_client.set_legal_hold.assert_called_once_with(True)


def test_azure_get_returns_bytes() -> None:
    container = MagicMock()
    blob_client = MagicMock()
    blob_client.download_blob.return_value.readall.return_value = b"blob-data"
    container.get_blob_client.return_value = blob_client
    adapter = AzureWormAdapter(container_client=container)
    assert adapter.get("cap1") == b"blob-data"


def test_azure_verify_integrity_ok() -> None:
    container = MagicMock()
    blob_client = MagicMock()
    container.get_blob_client.return_value = blob_client
    adapter = AzureWormAdapter(container_client=container)
    result = adapter.verify_integrity("cap1")
    assert result.ok


def test_azure_verify_integrity_fail() -> None:
    container = MagicMock()
    blob_client = MagicMock()
    blob_client.get_blob_properties.side_effect = Exception("BlobNotFound")
    container.get_blob_client.return_value = blob_client
    adapter = AzureWormAdapter(container_client=container)
    result = adapter.verify_integrity("cap1")
    assert not result.ok
    assert "BlobNotFound" in result.error


def test_azure_implements_protocol() -> None:
    container = MagicMock()
    adapter = AzureWormAdapter(container_client=container)
    assert isinstance(adapter, WormAdapter)


# ---------------------------------------------------------------------------
# GCS adapter
# ---------------------------------------------------------------------------

def test_gcs_put_uploads() -> None:
    bucket = MagicMock()
    blob = MagicMock()
    blob.generation = 12345
    bucket.blob.return_value = blob
    adapter = GCSWormAdapter(bucket=bucket)
    receipt = adapter.put("cap1", b"data", retention_days=30)
    assert receipt.backend_type == "gcs"
    assert receipt.backend_confirmation_token == "12345"
    blob.upload_from_string.assert_called_once_with(b"data")


def test_gcs_put_generation_none_falls_back_to_empty_string() -> None:
    bucket = MagicMock()
    blob = MagicMock()
    blob.generation = None
    bucket.blob.return_value = blob
    adapter = GCSWormAdapter(bucket=bucket)
    receipt = adapter.put("cap1", b"data", retention_days=1)
    assert receipt.backend_confirmation_token == ""


def test_gcs_lock_sets_hold() -> None:
    bucket = MagicMock()
    blob = MagicMock()
    bucket.blob.return_value = blob
    adapter = GCSWormAdapter(bucket=bucket)
    adapter.lock("cap1", "hold-001")
    assert blob.temporary_hold is True
    blob.patch.assert_called_once()


def test_gcs_verify_not_found() -> None:
    bucket = MagicMock()
    blob = MagicMock()
    blob.exists.return_value = False
    bucket.blob.return_value = blob
    adapter = GCSWormAdapter(bucket=bucket)
    result = adapter.verify_integrity("missing")
    assert not result.ok
    assert "not found" in result.error


def test_gcs_verify_ok() -> None:
    bucket = MagicMock()
    blob = MagicMock()
    blob.exists.return_value = True
    bucket.blob.return_value = blob
    adapter = GCSWormAdapter(bucket=bucket)
    result = adapter.verify_integrity("cap1")
    assert result.ok


def test_gcs_verify_exception() -> None:
    bucket = MagicMock()
    blob = MagicMock()
    blob.exists.side_effect = Exception("network error")
    bucket.blob.return_value = blob
    adapter = GCSWormAdapter(bucket=bucket)
    result = adapter.verify_integrity("cap1")
    assert not result.ok
    assert "network error" in result.error


def test_gcs_list_returns_entries() -> None:
    bucket = MagicMock()
    b1, b2 = MagicMock(), MagicMock()
    b1.name, b1.size = "cap1", 100
    b2.name, b2.size = "cap2", 200
    bucket.list_blobs.return_value = [b1, b2]
    adapter = GCSWormAdapter(bucket=bucket)
    entries = adapter.list()
    assert len(entries) == 2
    assert {e.capsule_id for e in entries} == {"cap1", "cap2"}


def test_gcs_implements_protocol() -> None:
    bucket = MagicMock()
    adapter = GCSWormAdapter(bucket=bucket)
    assert isinstance(adapter, WormAdapter)
