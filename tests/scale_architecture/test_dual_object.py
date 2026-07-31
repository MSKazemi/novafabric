"""Tests for DualObjectStore (cap-003, ADR-0066)."""

from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.storage.dual_object_store import DualObjectStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_CAPSULE: dict[str, object] = {
    "run_id": "run-abc",
    "event_type": "RunCompleted",
    "output_text": "The model said hello.",
    "arguments": {"tool": "read_file", "path": "/etc/passwd"},
    "context_snapshot": {"messages": ["foo", "bar"]},
    "duration_ms": 1234,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDualObjectSplitWithPII:
    def test_audit_record_redacted(self, tmp_path: Path) -> None:
        store = DualObjectStore.__new__(DualObjectStore)
        store.enabled = True

        store.split_and_store_local("run-abc", SAMPLE_CAPSULE, tmp_path)

        audit_data = json.loads((tmp_path / "run-abc_audit.json").read_text())
        assert audit_data["output_text"] == "[REDACTED]"
        assert audit_data["arguments"] == "[REDACTED]"
        assert audit_data["context_snapshot"] == "[REDACTED]"
        # Non-PII fields must be preserved
        assert audit_data["duration_ms"] == 1234

    def test_pii_payload_has_original(self, tmp_path: Path) -> None:
        store = DualObjectStore.__new__(DualObjectStore)
        store.enabled = True

        store.split_and_store_local("run-abc", SAMPLE_CAPSULE, tmp_path)

        pii_data = json.loads((tmp_path / "run-abc_pii.json").read_text())
        assert pii_data["output_text"] == "The model said hello."
        assert "arguments" in pii_data

    def test_result_keys_when_enabled(self, tmp_path: Path) -> None:
        store = DualObjectStore.__new__(DualObjectStore)
        store.enabled = True

        result = store.split_and_store_local("run-abc", SAMPLE_CAPSULE, tmp_path)

        assert result.cap003_enabled is True
        assert result.pii_object_key is not None
        assert result.pii_content_digest is not None


class TestDualObjectSplitDisabled:
    def test_no_split_when_disabled(self, tmp_path: Path) -> None:
        store = DualObjectStore.__new__(DualObjectStore)
        store.enabled = False

        result = store.split_and_store_local("run-xyz", SAMPLE_CAPSULE, tmp_path)

        assert result.pii_object_key is None
        assert result.pii_content_digest is None
        assert result.cap003_enabled is False

    def test_env_flag_defaults_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Changed to "true" in v0.44.0 on a mis-citation (ADR-0069 resolves a
        # different capability's OQ-01). Restored to "false" 2026-07-30 per
        # SCALE-ADR-003's mandatory safety gate pending real legal-counsel review.
        monkeypatch.delenv("NOVA_CAP003_ENABLED", raising=False)
        store = DualObjectStore()
        assert store.enabled is False

    def test_env_flag_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_CAP003_ENABLED", "true")
        store = DualObjectStore()
        assert store.enabled is True


class TestDigestComputation:
    def test_sha256_fallback(self, tmp_path: Path) -> None:
        store = DualObjectStore.__new__(DualObjectStore)
        store.enabled = True

        # Force SHA-256 path by hiding blake3
        with patch.dict("sys.modules", {"blake3": None}):
            result = store.split_and_store_local("run-sha", SAMPLE_CAPSULE, tmp_path)

        pii_bytes = (tmp_path / "run-sha_pii.json").read_bytes()
        expected = hashlib.sha256(pii_bytes).hexdigest()
        assert result.pii_content_digest == expected

    def test_blake3_used_when_available(self, tmp_path: Path) -> None:
        fake_blake3_mod = types.ModuleType("blake3")

        class _FakeHasher:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def hexdigest(self) -> str:
                return "blake3:" + hashlib.sha256(self._data).hexdigest()

        fake_blake3_mod.blake3 = _FakeHasher  # type: ignore[attr-defined]

        store = DualObjectStore.__new__(DualObjectStore)
        store.enabled = True

        with patch.dict("sys.modules", {"blake3": fake_blake3_mod}):
            result = store.split_and_store_local("run-b3", SAMPLE_CAPSULE, tmp_path)

        assert result.pii_content_digest is not None
        assert result.pii_content_digest.startswith("blake3:")


class TestRedactHelper:
    """Tests for _redact recursive logic."""

    def test_redact_nested_pii_key(self) -> None:
        nested = {"outer": {"output_text": "secret", "safe": "ok"}}
        result = DualObjectStore._redact(nested)
        assert isinstance(result, dict)
        assert result["outer"]["output_text"] == "[REDACTED]"  # type: ignore[index]
        assert result["outer"]["safe"] == "ok"  # type: ignore[index]

    def test_redact_list_with_dicts(self) -> None:
        data = [{"output_text": "pii"}, {"safe": "value"}]
        result = DualObjectStore._redact(data)
        assert isinstance(result, list)
        assert result[0]["output_text"] == "[REDACTED]"  # type: ignore[index]
        assert result[1]["safe"] == "value"  # type: ignore[index]

    def test_redact_scalar(self) -> None:
        assert DualObjectStore._redact("unchanged") == "unchanged"
        assert DualObjectStore._redact(42) == 42
