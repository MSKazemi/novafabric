# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for GDPR Art.30 RoPA exporter (cap-007)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cap-ropa-001"
    d.mkdir()
    manifest = {
        "schema_version": "1.0.0",
        "run_id": "run-ropa-001",
        "asset_id": "asset-ropa-001",
        "status": "production",
        "capture_level": "standard",
        "model": "gpt-4o",
        "provider": "openai",
    }
    with open(d / "capsule.yaml", "w") as f:
        yaml.dump(manifest, f)
    return d


@pytest.fixture()
def capsule_dir_with_redaction(capsule_dir: Path) -> Path:
    redaction = {
        "total_detections": 10,
        "redacted_count": 10,
        "redacted_items": [
            {"pii_type": "EMAIL", "redacted": True},
            {"pii_type": "PHONE", "redacted": True},
        ],
    }
    (capsule_dir / "redaction_manifest.json").write_text(json.dumps(redaction))
    return capsule_dir


class TestGDPRRoPAExporter:
    def test_build_ropa_entry_minimal(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        exporter = GDPRRoPAExporter()
        entry = exporter.build_ropa_entry(capsule_dir)

        assert entry.run_id == "run-ropa-001"
        assert entry.asset_id == "asset-ropa-001"
        assert entry.completeness == "partial"
        assert "controller_name" in entry.missing_fields
        assert "controller_contact" in entry.missing_fields

    def test_build_ropa_entry_with_operator_config(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        exporter = GDPRRoPAExporter(
            operator_config={
                "controller_name": "Acme Corp",
                "controller_contact": "dpo@acme.com",
            }
        )
        entry = exporter.build_ropa_entry(capsule_dir)

        assert entry.controller_name == "Acme Corp"
        assert entry.controller_contact == "dpo@acme.com"
        assert entry.completeness == "complete"
        assert entry.missing_fields == []

    def test_security_measures_always_includes_dsse(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        entry = GDPRRoPAExporter().build_ropa_entry(capsule_dir)
        assert "dsse_envelope_signing" in entry.security_measures

    def test_evidence_bundle_adds_integrity_measure(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        (capsule_dir / "evidence_bundle.json").write_text("{}")
        entry = GDPRRoPAExporter().build_ropa_entry(capsule_dir)
        assert "evidence_bundle_integrity" in entry.security_measures

    def test_personal_data_categories_from_redaction(
        self, capsule_dir_with_redaction: Path
    ) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        entry = GDPRRoPAExporter().build_ropa_entry(capsule_dir_with_redaction)
        assert "email" in entry.personal_data_categories
        assert "phone" in entry.personal_data_categories

    def test_build_ropa_document_multiple_capsules(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        cap2 = tmp_path / "cap-ropa-002"
        cap2.mkdir()
        import yaml as _yaml
        with open(cap2 / "capsule.yaml", "w") as f:
            _yaml.dump({"run_id": "run-002"}, f)

        exporter = GDPRRoPAExporter(
            operator_config={"controller_name": "Acme", "controller_contact": "dpo@acme.com"}
        )
        doc = exporter.build_ropa_document([capsule_dir, cap2])
        assert doc.total_entries == 2
        assert len(doc.entries) == 2

    def test_export_json_produces_valid_jsonld(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        exporter = GDPRRoPAExporter(
            operator_config={"controller_name": "Acme", "controller_contact": "dpo@acme.com"}
        )
        doc = exporter.build_ropa_document([capsule_dir])
        out = tmp_path / "ropa.jsonld"
        exporter.export_json(doc, out)

        assert out.exists()
        data = json.loads(out.read_text())
        assert data["@type"] == "gdpr:RecordsOfProcessingActivities"
        assert "nova:controllerName" in data
        assert len(data["gdpr:hasProcessingActivity"]) == 1

    def test_missing_capsule_yaml_returns_unknown_run(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        empty_dir = tmp_path / "empty-cap"
        empty_dir.mkdir()
        entry = GDPRRoPAExporter().build_ropa_entry(empty_dir)
        assert entry.run_id == "unknown"

    def test_third_country_transfers_from_config(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

        exporter = GDPRRoPAExporter(
            operator_config={
                "controller_name": "Acme",
                "controller_contact": "dpo@acme.com",
                "third_country_transfers": [
                    {
                        "recipient": "US Analytics Inc",
                        "country": "US",
                        "safeguard": "standard_contractual_clauses",
                    }
                ],
            }
        )
        entry = exporter.build_ropa_entry(capsule_dir)
        assert len(entry.third_country_transfers) == 1
        assert entry.third_country_transfers[0].country == "US"
