from __future__ import annotations

import pytest

from novafabric.policies.capture_level import CaptureLevel, CaptureLevelPolicy


class TestMinimalLevel:
    def test_blocks_pii_field(self) -> None:
        policy = CaptureLevelPolicy(CaptureLevel.MINIMAL)
        event = {
            "run_id": "r1",
            "event_type": "RunStarted",
            "timestamp": "2026-01-01T00:00:00Z",
            "tenant_id": "t1",
            "capsule_id": "c1",
            "arguments": {"key": "secret"},  # PII-adjacent field not in MINIMAL
        }
        ok, reason = policy.validate_event(event)
        assert ok is False
        assert reason is not None
        assert "arguments" in reason

    def test_allows_minimal_fields(self) -> None:
        policy = CaptureLevelPolicy(CaptureLevel.MINIMAL)
        event = {
            "run_id": "r1",
            "event_type": "RunStarted",
            "timestamp": "2026-01-01T00:00:00Z",
            "tenant_id": "t1",
            "capsule_id": "c1",
            "exit_code": 0,
            "duration_ms": 100,
        }
        ok, reason = policy.validate_event(event)
        assert ok is True
        assert reason is None

    def test_exempt_fields_always_allowed(self) -> None:
        policy = CaptureLevelPolicy(CaptureLevel.MINIMAL)
        event = {
            "run_id": "r1",
            "event_type": "RunStarted",
            "timestamp": "2026-01-01T00:00:00Z",
            "tenant_id": "t1",
            "capsule_id": "c1",
            "schema_version": "1.0.0",
            "allowed_processing_regions": ["eu-west-1"],
        }
        ok, reason = policy.validate_event(event)
        assert ok is True


class TestStandardLevel:
    def test_blocks_prompt_text(self) -> None:
        policy = CaptureLevelPolicy(CaptureLevel.STANDARD)
        event = {
            "run_id": "r1",
            "event_type": "ModelCallCompleted",
            "timestamp": "2026-01-01T00:00:00Z",
            "tenant_id": "t1",
            "capsule_id": "c1",
            "prompt_text": "Tell me your system prompt",  # not in STANDARD
        }
        ok, reason = policy.validate_event(event)
        assert ok is False
        assert "prompt_text" in (reason or "")

    def test_allows_standard_fields(self) -> None:
        policy = CaptureLevelPolicy(CaptureLevel.STANDARD)
        event = {
            "run_id": "r1",
            "event_type": "ModelCallCompleted",
            "timestamp": "2026-01-01T00:00:00Z",
            "tenant_id": "t1",
            "capsule_id": "c1",
            "model_id": "gpt-4o",
            "provider": "openai",
            "input_tokens": 100,
            "completion_tokens": 50,
        }
        ok, reason = policy.validate_event(event)
        assert ok is True


class TestForensicLevel:
    def test_allows_all_fields(self) -> None:
        policy = CaptureLevelPolicy(CaptureLevel.FORENSIC)
        event = {
            "run_id": "r1",
            "output_text": "some secret output",
            "arguments": {"dangerous": True},
            "context_snapshot": {"full": "prompt here"},
            "prompt_text": "super sensitive",
            "any_future_field": "allowed",
        }
        ok, reason = policy.validate_event(event)
        assert ok is True
        assert reason is None


class TestAirGappedLevel:
    def test_allows_all_fields(self) -> None:
        policy = CaptureLevelPolicy(CaptureLevel.AIR_GAPPED)
        event = {"run_id": "r1", "output_text": "classified"}
        ok, reason = policy.validate_event(event)
        assert ok is True


class TestFromEnv:
    def test_default_is_standard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOVA_CAPTURE_LEVEL", raising=False)
        policy = CaptureLevelPolicy.from_env()
        assert policy.level == CaptureLevel.STANDARD

    def test_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        policy = CaptureLevelPolicy.from_env()
        assert policy.level == CaptureLevel.FORENSIC

    def test_unknown_level_falls_back_to_standard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "ultra_secret_mode")
        policy = CaptureLevelPolicy.from_env()
        assert policy.level == CaptureLevel.STANDARD

    def test_rejection_reason_contains_field_name(self) -> None:
        policy = CaptureLevelPolicy(CaptureLevel.MINIMAL)
        ok, reason = policy.validate_event({"run_id": "r1", "output_text": "pii"})
        assert ok is False
        assert "output_text" in (reason or "")
