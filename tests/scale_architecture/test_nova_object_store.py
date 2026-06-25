"""Tests for NovaObjectStore (cap-009, ADR-0066)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from novafabric.storage.nova_object_store import (
    NovaObjectStore,
    ObjectLockNotSupportedError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_client_with_object_lock_enabled() -> MagicMock:
    client = MagicMock()
    client.get_bucket_object_lock_configuration.return_value = {
        "ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}
    }
    return client


def _mock_client_with_no_object_lock() -> MagicMock:
    client = MagicMock()
    client.get_bucket_object_lock_configuration.return_value = {
        "ObjectLockConfiguration": {"ObjectLockEnabled": "Disabled"}
    }
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidate:
    def test_success_when_object_lock_enabled(self) -> None:
        store = NovaObjectStore(bucket="test-bucket")
        store._client = _mock_client_with_object_lock_enabled()

        info = store.validate()

        assert info["bucket"] == "test-bucket"
        assert info["object_lock"] == "COMPLIANCE"

    def test_raises_when_object_lock_disabled(self) -> None:
        store = NovaObjectStore(bucket="test-bucket")
        store._client = _mock_client_with_no_object_lock()

        with pytest.raises(ObjectLockNotSupportedError):
            store.validate()

    def test_raises_on_missing_configuration_error(self) -> None:
        client = MagicMock()

        # Simulate boto3 raising an error whose class name contains the sentinel string
        class ObjectLockConfigurationNotFoundError(Exception):
            pass

        client.get_bucket_object_lock_configuration.side_effect = (
            ObjectLockConfigurationNotFoundError("no config")
        )
        store = NovaObjectStore(bucket="locked-bucket")
        store._client = client

        with pytest.raises(ObjectLockNotSupportedError):
            store.validate()


class TestPutObject:
    def test_calls_boto3_put_object(self) -> None:
        client = MagicMock()
        client.put_object.return_value = {"ETag": '"abc123"'}

        store = NovaObjectStore(bucket="my-bucket")
        store._client = client

        etag = store.put_object("key/path.json", b"data", metadata={"run_id": "r1"})

        client.put_object.assert_called_once_with(
            Bucket="my-bucket",
            Key="key/path.json",
            Body=b"data",
            Metadata={"run_id": "r1"},
        )
        assert etag == '"abc123"'

    def test_put_without_metadata(self) -> None:
        client = MagicMock()
        client.put_object.return_value = {"ETag": '"xyz"'}

        store = NovaObjectStore(bucket="b")
        store._client = client

        store.put_object("key.json", b"hello")
        call_kwargs = client.put_object.call_args.kwargs
        assert "Metadata" not in call_kwargs


class TestDeleteObject:
    def test_calls_boto3_delete(self) -> None:
        client = MagicMock()
        store = NovaObjectStore(bucket="b")
        store._client = client

        store.delete_object("pii-key.json")

        client.delete_object.assert_called_once_with(Bucket="b", Key="pii-key.json")


class TestEnvVarInit:
    def test_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_S3_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("NOVA_S3_BUCKET", "my-capsules")
        monkeypatch.setenv("NOVA_S3_ACCESS_KEY", "ak")
        monkeypatch.setenv("NOVA_S3_SECRET_KEY", "sk")

        store = NovaObjectStore()

        assert store.endpoint_url == "http://minio:9000"
        assert store.bucket == "my-capsules"
        assert store.access_key == "ak"
        assert store.secret_key == "sk"

    def test_default_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
        store = NovaObjectStore()
        assert store.bucket == "nova-capsules"

    def test_constructor_params_override_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_S3_ENDPOINT_URL", "http://env-endpoint")
        store = NovaObjectStore(endpoint_url="http://explicit-endpoint")
        assert store.endpoint_url == "http://explicit-endpoint"


class TestGetClient:
    """Tests for the lazy boto3 client initialization path."""

    def test_raises_import_error_when_boto3_missing(self) -> None:
        import sys
        from unittest.mock import patch

        store = NovaObjectStore(bucket="b")
        with patch.dict(sys.modules, {"boto3": None}):
            with pytest.raises(ImportError, match="boto3 required"):
                store._get_client()

    def test_client_cached_on_second_call(self) -> None:
        store = NovaObjectStore(bucket="b")
        fake_client = MagicMock()
        store._client = fake_client
        assert store._get_client() is fake_client

    def test_get_object_metadata(self) -> None:
        client = MagicMock()
        client.head_object.return_value = {"ContentLength": 512, "ETag": '"etag1"'}

        store = NovaObjectStore(bucket="b")
        store._client = client

        meta = store.get_object_metadata("my/key.json")
        assert meta["key"] == "my/key.json"
        assert meta["size"] == 512
        assert meta["etag"] == '"etag1"'

    def test_validate_non_object_lock_error_is_reraised(self) -> None:
        """Non-ObjectLock errors should propagate unchanged."""
        client = MagicMock()
        client.get_bucket_object_lock_configuration.side_effect = RuntimeError(
            "network error"
        )

        store = NovaObjectStore(bucket="b")
        store._client = client

        with pytest.raises(RuntimeError, match="network error"):
            store.validate()

    def test_get_client_uses_boto3_with_credentials(self) -> None:
        """_get_client passes endpoint_url, access_key, secret_key to boto3.client."""
        import sys
        import types
        from unittest.mock import patch

        fake_client = MagicMock()
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]

        store = NovaObjectStore(
            endpoint_url="http://localhost:9000",
            bucket="b",
            access_key="ak",
            secret_key="sk",
        )
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            client = store._get_client()

        assert client is fake_client
        fake_boto3.client.assert_called_once_with(
            service_name="s3",
            endpoint_url="http://localhost:9000",
            aws_access_key_id="ak",
            aws_secret_access_key="sk",
        )

    def test_get_client_no_credentials(self) -> None:
        """_get_client omits credentials when not set."""
        import sys
        import types
        from unittest.mock import patch

        fake_client = MagicMock()
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]

        store = NovaObjectStore(bucket="b")
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            client = store._get_client()

        assert client is fake_client
        call_kwargs = fake_boto3.client.call_args.kwargs
        assert "aws_access_key_id" not in call_kwargs
        assert "endpoint_url" not in call_kwargs
