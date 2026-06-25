"""Tests for DualObjectStore S3 backend routing (cap-003, ADR-0066)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.storage.dual_object_store import DualObjectStore

SAMPLE_CAPSULE: dict[str, object] = {
    "run_id": "run-s3-test",
    "event_type": "RunCompleted",
    "output_text": "secret output",
    "arguments": {"tool": "calculator"},
    "duration_ms": 999,
}


# ---------------------------------------------------------------------------
# _get_s3_backends tests
# ---------------------------------------------------------------------------


class TestGetS3Backends:
    def test_returns_none_when_no_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_S3_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
        compliance, governance = DualObjectStore._get_s3_backends()
        assert compliance is None
        assert governance is None

    def test_returns_stores_when_bucket_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_S3_BUCKET", "nova-capsules")
        monkeypatch.delenv("NOVA_S3_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("NOVA_S3_COMPLIANCE_BUCKET", raising=False)
        monkeypatch.delenv("NOVA_S3_GOVERNANCE_BUCKET", raising=False)

        compliance, governance = DualObjectStore._get_s3_backends()
        assert compliance is not None
        assert governance is not None
        assert compliance.bucket == "nova-capsules"
        assert governance.bucket == "nova-capsules"

    def test_returns_stores_when_endpoint_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_S3_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
        monkeypatch.delenv("NOVA_S3_COMPLIANCE_BUCKET", raising=False)
        monkeypatch.delenv("NOVA_S3_GOVERNANCE_BUCKET", raising=False)

        compliance, governance = DualObjectStore._get_s3_backends()
        assert compliance is not None
        assert compliance.endpoint_url == "http://minio:9000"

    def test_uses_compliance_and_governance_buckets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_S3_ENDPOINT_URL", "http://s3:9000")
        monkeypatch.setenv("NOVA_S3_COMPLIANCE_BUCKET", "compliance-worm")
        monkeypatch.setenv("NOVA_S3_GOVERNANCE_BUCKET", "governance-mutable")
        monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)

        compliance, governance = DualObjectStore._get_s3_backends()
        assert compliance is not None
        assert governance is not None
        assert compliance.bucket == "compliance-worm"
        assert governance.bucket == "governance-mutable"


# ---------------------------------------------------------------------------
# split_and_store tests (S3 path with mocked NovaObjectStore)
# ---------------------------------------------------------------------------


def _make_store_with_s3_enabled(monkeypatch: pytest.MonkeyPatch, enabled: bool = True):
    """Set up environment for S3 mode and return a DualObjectStore."""
    monkeypatch.setenv("NOVA_S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("NOVA_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("NOVA_CAP003_ENABLED", "true" if enabled else "false")
    return DualObjectStore()


class TestSplitAndStoreS3:
    def test_puts_audit_record_to_compliance_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _make_store_with_s3_enabled(monkeypatch)

        compliance_mock = MagicMock()
        governance_mock = MagicMock()

        with patch.object(
            DualObjectStore,
            "_get_s3_backends",
            return_value=(compliance_mock, governance_mock),
        ):
            store.split_and_store("run-abc", SAMPLE_CAPSULE)

        compliance_mock.put_object.assert_called_once()
        call_kwargs = compliance_mock.put_object.call_args
        key: str = call_kwargs.args[0]
        data: bytes = call_kwargs.args[1]
        assert "audit/run-abc" in key
        audit_data = json.loads(data)
        assert audit_data["output_text"] == "[REDACTED]"
        assert audit_data["duration_ms"] == 999

    def test_puts_pii_payload_to_governance_store_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _make_store_with_s3_enabled(monkeypatch, enabled=True)

        compliance_mock = MagicMock()
        governance_mock = MagicMock()

        with patch.object(
            DualObjectStore,
            "_get_s3_backends",
            return_value=(compliance_mock, governance_mock),
        ):
            result = store.split_and_store("run-abc", SAMPLE_CAPSULE)

        governance_mock.put_object.assert_called_once()
        key: str = governance_mock.put_object.call_args.args[0]
        assert "pii/run-abc" in key
        assert result.pii_object_key is not None
        assert result.pii_content_digest is not None
        assert result.cap003_enabled is True

    def test_no_pii_write_when_cap003_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _make_store_with_s3_enabled(monkeypatch, enabled=False)

        compliance_mock = MagicMock()
        governance_mock = MagicMock()

        with patch.object(
            DualObjectStore,
            "_get_s3_backends",
            return_value=(compliance_mock, governance_mock),
        ):
            result = store.split_and_store("run-xyz", SAMPLE_CAPSULE)

        # Audit should be written
        compliance_mock.put_object.assert_called_once()
        # PII should NOT be written
        governance_mock.put_object.assert_not_called()
        assert result.pii_object_key is None
        assert result.cap003_enabled is False

    def test_result_audit_key_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _make_store_with_s3_enabled(monkeypatch, enabled=True)

        compliance_mock = MagicMock()
        governance_mock = MagicMock()

        with patch.object(
            DualObjectStore,
            "_get_s3_backends",
            return_value=(compliance_mock, governance_mock),
        ):
            result = store.split_and_store("run-42", SAMPLE_CAPSULE)

        assert result.audit_object_key == "audit/run-42/audit.json"
        assert result.pii_object_key == "pii/run-42/pii.json"


# ---------------------------------------------------------------------------
# split_and_store fallback to local filesystem
# ---------------------------------------------------------------------------


class TestSplitAndStoreFallbackToLocal:
    def test_uses_local_when_s3_not_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("NOVA_S3_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
        monkeypatch.setenv("NOVA_CAP003_ENABLED", "true")

        store = DualObjectStore()
        result = store.split_and_store("run-local", SAMPLE_CAPSULE, output_dir=tmp_path)

        assert (tmp_path / "run-local_audit.json").exists()
        assert result.cap003_enabled is True

    def test_raises_when_s3_not_configured_and_no_output_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_S3_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)

        store = DualObjectStore()
        with pytest.raises(ValueError, match="output_dir is required"):
            store.split_and_store("run-fail", SAMPLE_CAPSULE)
