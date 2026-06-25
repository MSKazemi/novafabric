# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for GCS WORM adapter (GcsWormAdapter)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from novafabric.object_capsule_store.exceptions import CASMismatchError
from novafabric.object_capsule_store.worm.base import (
    ConditionalPutConflict,
    WormAdapter,
    WormPutResult,
)
from novafabric.object_capsule_store.worm.gcs import GcsWormAdapter

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_BUCKET = "test-bucket"
_KEY = "capsules/tenant/abcd/abcd1234/data.zst"
_DATA = b"hello capsule"
_SHA256 = hashlib.sha256(_DATA).hexdigest()
_RETENTION_DAYS = 365


def _make_adapter() -> tuple[GcsWormAdapter, MagicMock]:
    """Return (adapter, mock_client) with a pre-wired MagicMock GCS client."""
    mock_client = MagicMock()
    # blob.exists() returns True by default; override per test
    mock_blob = MagicMock()
    mock_blob.generation = "1234567890"
    mock_blob.exists.return_value = True
    mock_blob.download_as_bytes.return_value = _DATA
    mock_blob.retention = MagicMock()
    mock_blob.retention.mode = None
    mock_blob.retention.retain_until_time = None

    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client.bucket.return_value = mock_bucket

    # list_blobs returns an iterable of blob-like objects
    blob_entry = MagicMock()
    blob_entry.name = _KEY
    mock_client.list_blobs.return_value = [blob_entry]

    adapter = GcsWormAdapter(bucket_name=_BUCKET, client=mock_client)
    return adapter, mock_client


# ---------------------------------------------------------------------------
# 1. put_object — happy path
# ---------------------------------------------------------------------------

class TestPutObject:
    def test_returns_worm_put_result(self) -> None:
        adapter, mock_client = _make_adapter()
        result = adapter.put_object(_KEY, _DATA, _SHA256, _RETENTION_DAYS)

        assert isinstance(result, WormPutResult)
        assert result.key == _KEY
        assert result.confirmation_token == "1234567890"
        assert result.locked_until_ms > 0

    def test_upload_called_with_correct_args(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        adapter.put_object(_KEY, _DATA, _SHA256, _RETENTION_DAYS)

        blob.upload_from_string.assert_called_once_with(
            _DATA, content_type="application/octet-stream"
        )

    def test_retention_locked_mode_set(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        adapter.put_object(_KEY, _DATA, _SHA256, _RETENTION_DAYS)

        assert blob.retention.mode == "LOCKED"
        blob.patch.assert_called_once()

    def test_locked_until_ms_in_future(self) -> None:
        adapter, mock_client = _make_adapter()
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        result = adapter.put_object(_KEY, _DATA, _SHA256, _RETENTION_DAYS)

        assert result.locked_until_ms > now_ms

    def test_custom_content_type(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        adapter.put_object(_KEY, _DATA, _SHA256, _RETENTION_DAYS, content_type="application/json")

        blob.upload_from_string.assert_called_once_with(
            _DATA, content_type="application/json"
        )

    def test_cas_mismatch_error_on_upload_failure(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value
        blob.upload_from_string.side_effect = Exception("400 checksum does not match")

        with pytest.raises(CASMismatchError):
            adapter.put_object(_KEY, _DATA, _SHA256, _RETENTION_DAYS)

    def test_non_checksum_exception_propagates(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value
        blob.upload_from_string.side_effect = RuntimeError("network error")

        with pytest.raises(RuntimeError, match="network error"):
            adapter.put_object(_KEY, _DATA, _SHA256, _RETENTION_DAYS)


# ---------------------------------------------------------------------------
# 2. apply_identical — uploads two objects with same policy
# ---------------------------------------------------------------------------

class TestApplyIdentical:
    def test_returns_two_results(self) -> None:
        adapter, mock_client = _make_adapter()
        key_b = "capsules/tenant/abcd/abcd1234/novaseal.json"
        data_b = b'{"sig": "abc"}'
        sha256_b = hashlib.sha256(data_b).hexdigest()

        result_a, result_b = adapter.apply_identical(
            _KEY, _DATA, _SHA256,
            key_b, data_b, sha256_b,
            _RETENTION_DAYS,
        )

        assert isinstance(result_a, WormPutResult)
        assert isinstance(result_b, WormPutResult)

    def test_second_object_uses_json_content_type(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value
        key_b = "capsules/tenant/abcd/abcd1234/novaseal.json"
        data_b = b'{"sig": "abc"}'
        sha256_b = hashlib.sha256(data_b).hexdigest()

        adapter.apply_identical(
            _KEY, _DATA, _SHA256,
            key_b, data_b, sha256_b,
            _RETENTION_DAYS,
        )

        calls = blob.upload_from_string.call_args_list
        assert len(calls) == 2
        # Second call must use application/json
        assert calls[1].kwargs.get("content_type") == "application/json" or \
               calls[1].args[1] == "application/json" if len(calls[1].args) > 1 else \
               calls[1].kwargs.get("content_type") == "application/json"


# ---------------------------------------------------------------------------
# 3. get_object
# ---------------------------------------------------------------------------

class TestGetObject:
    def test_returns_bytes(self) -> None:
        adapter, mock_client = _make_adapter()
        result = adapter.get_object(_KEY)

        assert result == _DATA

    def test_calls_download_as_bytes(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        adapter.get_object(_KEY)

        blob.download_as_bytes.assert_called_once()


# ---------------------------------------------------------------------------
# 4. put_log_object
# ---------------------------------------------------------------------------

class TestPutLogObject:
    def test_returns_generation_token(self) -> None:
        adapter, mock_client = _make_adapter()
        token = adapter.put_log_object(_KEY, _DATA)

        assert token == "1234567890"

    def test_uploads_with_json_content_type(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        adapter.put_log_object(_KEY, _DATA)

        blob.upload_from_string.assert_called_once_with(
            _DATA, content_type="application/json"
        )

    def test_no_retention_applied(self) -> None:
        """Log objects must NOT have WORM retention applied."""
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        adapter.put_log_object(_KEY, _DATA)

        blob.patch.assert_not_called()


# ---------------------------------------------------------------------------
# 5. put_log_object_if_absent — conditional PUT
# ---------------------------------------------------------------------------

class TestPutLogObjectIfAbsent:
    def test_success_returns_token(self) -> None:
        adapter, mock_client = _make_adapter()
        token = adapter.put_log_object_if_absent(_KEY, _DATA)

        assert token == "1234567890"

    def test_calls_with_if_generation_match_zero(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        adapter.put_log_object_if_absent(_KEY, _DATA)

        blob.upload_from_string.assert_called_once_with(
            _DATA,
            content_type="application/json",
            if_generation_match=0,
        )

    def test_precondition_failed_raises_conflict(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        # Simulate PreconditionFailed by raising a generic exception with 412 in message
        # (avoids importing google.api_core in tests)
        blob.upload_from_string.side_effect = Exception("412 conditionNotMet")

        with pytest.raises(ConditionalPutConflict):
            adapter.put_log_object_if_absent(_KEY, _DATA)

    def test_other_exception_propagates(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value
        blob.upload_from_string.side_effect = RuntimeError("quota exceeded")

        with pytest.raises(RuntimeError, match="quota exceeded"):
            adapter.put_log_object_if_absent(_KEY, _DATA)


# ---------------------------------------------------------------------------
# 6. list_objects
# ---------------------------------------------------------------------------

class TestListObjects:
    def test_returns_sorted_keys(self) -> None:
        adapter, mock_client = _make_adapter()
        # Wire up two blob entries out of order
        b1, b2 = MagicMock(), MagicMock()
        b1.name = "capsules/t/zzzz/data.zst"
        b2.name = "capsules/t/aaaa/data.zst"
        mock_client.list_blobs.return_value = [b1, b2]

        result = adapter.list_objects("capsules/t/")

        assert result == sorted(["capsules/t/zzzz/data.zst", "capsules/t/aaaa/data.zst"])

    def test_passes_prefix_to_client(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.list_blobs.return_value = []

        adapter.list_objects("my/prefix/")

        mock_client.list_blobs.assert_called_once_with(_BUCKET, prefix="my/prefix/")

    def test_empty_prefix_returns_all(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.list_blobs.return_value = []

        result = adapter.list_objects("")

        assert result == []


# ---------------------------------------------------------------------------
# 7. delete_object
# ---------------------------------------------------------------------------

class TestDeleteObject:
    def test_calls_blob_delete(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value

        adapter.delete_object(_KEY)

        blob.delete.assert_called_once()

    def test_worm_locked_error_propagates(self) -> None:
        """GCS raises 403 when deleting a LOCKED object — must not be suppressed."""
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value
        blob.delete.side_effect = Exception("403 Forbidden: object is locked")

        with pytest.raises(Exception, match="403"):
            adapter.delete_object(_KEY)


# ---------------------------------------------------------------------------
# 8. object_exists
# ---------------------------------------------------------------------------

class TestObjectExists:
    def test_returns_true_when_blob_exists(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value
        blob.exists.return_value = True

        assert adapter.object_exists(_KEY) is True

    def test_returns_false_when_blob_absent(self) -> None:
        adapter, mock_client = _make_adapter()
        blob = mock_client.bucket.return_value.blob.return_value
        blob.exists.return_value = False

        assert adapter.object_exists(_KEY) is False


# ---------------------------------------------------------------------------
# Interface compliance
# ---------------------------------------------------------------------------

def test_gcs_is_worm_adapter_subclass() -> None:
    assert issubclass(GcsWormAdapter, WormAdapter)


def test_gcs_instantiates_with_mock_client() -> None:
    mock_client = MagicMock()
    adapter = GcsWormAdapter(bucket_name=_BUCKET, client=mock_client)
    assert adapter is not None
