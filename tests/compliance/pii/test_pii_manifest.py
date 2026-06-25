# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for RedactionManifest and RedactionManifestEntry models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from novafabric.compliance.pii.manifest import RedactionManifest, RedactionManifestEntry


def _entry(field_path: str = ".message", entity_type: str = "EMAIL") -> RedactionManifestEntry:
    return RedactionManifestEntry(
        field_path=field_path,
        detection_rule_id=entity_type,
        legal_basis="operator_declared_compliance",
        subject_id_hmac="sha256:" + "a" * 64,
        redacted_at_utc=datetime.now(timezone.utc).isoformat(),
    )


class TestRedactionManifestEntry:
    def test_fields_present(self) -> None:
        e = _entry()
        assert e.field_path == ".message"
        assert e.detection_rule_id == "EMAIL"
        assert e.legal_basis == "operator_declared_compliance"
        assert e.subject_id_hmac.startswith("sha256:")
        assert "redacted_at_utc" in e.model_dump()

    def test_frozen(self) -> None:
        e = _entry()
        with pytest.raises(Exception):
            e.field_path = "changed"  # type: ignore[misc]

    def test_json_roundtrip(self) -> None:
        e = _entry()
        restored = RedactionManifestEntry.model_validate_json(e.model_dump_json())
        assert restored == e


class TestRedactionManifest:
    def test_empty_manifest(self) -> None:
        m = RedactionManifest(capsule_id="CAP-001")
        assert m.entries == []
        # Active mode: legal_hold_mode defaults to False (OQ-01 resolved by ADR-0069).
        assert m.legal_hold_mode is False

    def test_append_returns_new_instance(self) -> None:
        m = RedactionManifest(capsule_id="CAP-001")
        e = _entry()
        m2 = m.append(e)
        assert m is not m2
        assert len(m.entries) == 0
        assert len(m2.entries) == 1

    def test_multiple_appends(self) -> None:
        m = RedactionManifest(capsule_id="CAP-001")
        m = m.append(_entry(".email"))
        m = m.append(_entry(".phone"))
        assert len(m.entries) == 2

    def test_legal_hold_mode_default_false(self) -> None:
        """Active mode (OQ-01 resolved by ADR-0069): legal_hold_mode defaults to False."""
        m = RedactionManifest(capsule_id="CAP-001")
        assert m.legal_hold_mode is False

    def test_json_roundtrip(self) -> None:
        m = RedactionManifest(capsule_id="CAP-001")
        m = m.append(_entry(".email"))
        restored = RedactionManifest.model_validate_json(m.model_dump_json())
        assert restored.capsule_id == m.capsule_id
        assert len(restored.entries) == 1
