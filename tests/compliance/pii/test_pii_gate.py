# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for PIIDetectionGate (cap-001)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from novafabric.compliance.pii.detector import PIISpan, RegexDetector
from novafabric.compliance.pii.gate import PIIDetectionGate, PIIScanError


class TestPIIDetectionGate:
    def test_email_redacted(self, pii_gate, tmp_path) -> None:
        payload = {"message": "Contact alice@example.com for details"}
        result = pii_gate.scan(payload, capsule_id="CAP-001")
        assert "alice@example.com" not in result.payload["message"]
        assert "[REDACTED:" in result.payload["message"]
        assert result.redacted_count == 1

    def test_manifest_appended_on_redaction(self, pii_gate, tmp_path) -> None:
        payload = {"text": "Call +1-555-123-4567 or email bob@test.org"}
        result = pii_gate.scan(payload, capsule_id="CAP-002")
        assert result.redacted_count >= 2
        assert len(result.manifest.entries) >= 2

    def test_no_pii_no_redaction(self, pii_gate) -> None:
        payload = {"text": "No personal information here", "count": 42}
        result = pii_gate.scan(payload, capsule_id="CAP-003")
        assert result.redacted_count == 0
        assert len(result.manifest.entries) == 0
        assert result.payload["text"] == "No personal information here"

    def test_fail_closed_on_detector_exception(self, tmp_path, pepper) -> None:
        """PIIScanError must abort pipeline — never swallowed."""

        class BrokenDetector:
            def detect(self, text: str) -> list[PIISpan]:
                raise RuntimeError("detector exploded")

        gate = PIIDetectionGate(
            detector=BrokenDetector(),
            pepper=pepper,
            legal_hold_dir=tmp_path / "legal_hold",
        )
        with pytest.raises(PIIScanError, match="PII scan failed"):
            gate.scan({"text": "some content"}, capsule_id="CAP-ERR")

    def test_legal_hold_mode_does_not_write_staging_file(self, pii_gate, tmp_path) -> None:
        """Active mode (OQ-01 resolved): no legal-hold staging file is written."""
        payload = {"contact": "alice@example.com"}
        pii_gate.scan(payload, capsule_id="CAP-HOLD")
        # Since _OQ01_UNRESOLVED=False, legal_hold staging is disabled.
        staging = tmp_path / "legal_hold" / "CAP-HOLD" / "redaction-manifest.json"
        assert not staging.exists(), (
            "Staging file should NOT be written in active mode (OQ-01 resolved by ADR-0069)"
        )

    def test_legal_hold_mode_flag_in_manifest_is_false(self, pii_gate) -> None:
        """Active mode: legal_hold_mode is False (OQ-01 resolved by ADR-0069)."""
        payload = {"email": "test@example.com"}
        result = pii_gate.scan(payload, capsule_id="CAP-LH")
        assert result.manifest.legal_hold_mode is False

    def test_subject_id_hmac_format(self, pii_gate) -> None:
        payload = {"user": "Hello John Smith, call 555-123-4567"}
        result = pii_gate.scan(payload, capsule_id="CAP-HMAC", subject_id="user-123")
        for entry in result.manifest.entries:
            assert entry.subject_id_hmac.startswith("sha256:")
            assert len(entry.subject_id_hmac) == 7 + 64  # "sha256:" + 64 hex chars

    def test_pepper_required_from_env(self, tmp_path) -> None:
        """PIIDetectionGate raises if NOVA_PII_PEPPER is not set."""
        with patch.dict(os.environ, {}, clear=True):
            if "NOVA_PII_PEPPER" in os.environ:
                del os.environ["NOVA_PII_PEPPER"]
            with pytest.raises(ValueError, match="NOVA_PII_PEPPER"):
                PIIDetectionGate(
                    detector=RegexDetector(),
                    pepper=None,
                    legal_hold_dir=tmp_path,
                )

    def test_pepper_env_var_works(self, tmp_path) -> None:
        with patch.dict(os.environ, {"NOVA_PII_PEPPER": "my-test-pepper"}):
            gate = PIIDetectionGate(
                detector=RegexDetector(),
                pepper=None,
                legal_hold_dir=tmp_path / "lh",
            )
        result = gate.scan({"msg": "no pii here"}, capsule_id="CAP-ENV")
        assert result.redacted_count == 0

    def test_nested_dict_redaction(self, pii_gate) -> None:
        payload = {
            "outer": {
                "inner": "contact alice@example.com please",
                "number": 42,
            }
        }
        result = pii_gate.scan(payload, capsule_id="CAP-NESTED")
        assert "alice@example.com" not in result.payload["outer"]["inner"]
        assert result.redacted_count >= 1

    def test_list_redaction(self, pii_gate) -> None:
        payload = {"contacts": ["alice@example.com", "no-pii-here", "bob@test.org"]}
        result = pii_gate.scan(payload, capsule_id="CAP-LIST")
        assert result.redacted_count >= 2
        assert "[REDACTED:" in result.payload["contacts"][0]

    def test_legal_hold_dir_from_novafabric_home(self, tmp_path) -> None:
        """NOVAFABRIC_HOME should set the default legal_hold_dir."""
        with patch.dict(os.environ, {"NOVA_PII_PEPPER": "test-pepper",
                                     "NOVAFABRIC_HOME": str(tmp_path / "nova_home")}):
            gate = PIIDetectionGate(
                detector=RegexDetector(),
                pepper=None,
                legal_hold_dir=None,
            )
        assert gate._legal_hold_dir == tmp_path / "nova_home" / "legal_hold"

    def test_pii_scan_error_is_reraised(self, tmp_path, pepper) -> None:
        """PIIScanError must propagate (not be caught by the outer except clause)."""
        class AlwaysPIIScanError:
            def detect(self, text: str) -> list[PIISpan]:
                from novafabric.compliance.pii.gate import PIIScanError
                raise PIIScanError("direct scan error")

        gate = PIIDetectionGate(
            detector=AlwaysPIIScanError(),
            pepper=pepper,
            legal_hold_dir=tmp_path / "lh",
        )
        with pytest.raises(PIIScanError):
            gate.scan({"text": "hello"}, capsule_id="CAP-RERAISE")
