# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""GDPR Art.30 Records of Processing Activities (RoPA) exporter (cap-007).

Implements the mandatory record-keeping obligation under Regulation (EU) 2016/679
Article 30. Derives RoPA entries from capsule metadata, evidence bundles, and
the asset registry.

Status: cap-007 ships with fields derivable from capsule data. Fields requiring
an external Data Protection Officer (DPO) registry are operator-declared stubs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class RoPATransfer(BaseModel):
    """GDPR Art.30(1)(e) — third country or international organisation transfer."""
    recipient: str
    country: str
    safeguard: str = "standard_contractual_clauses"
    reference: str = ""


class RoPAEntry(BaseModel):
    """A single Art.30 processing activity record.

    Maps to EU GDPR Art.30(1) and Art.30(2) obligations.
    """
    # Art.30(1)(a)
    controller_name: str
    controller_contact: str
    # Art.30(1)(b)
    processing_purposes: list[str]
    legal_basis: str = "legitimate_interests"
    # Art.30(1)(c)
    data_subject_categories: list[str]
    # Art.30(1)(d)
    personal_data_categories: list[str]
    # Art.30(1)(e)
    recipient_categories: list[str] = Field(default_factory=list)
    third_country_transfers: list[RoPATransfer] = Field(default_factory=list)
    # Art.30(1)(f)
    retention_period: str = "until_capsule_deletion"
    # Art.30(1)(g)
    security_measures: list[str] = Field(default_factory=list)
    # Derived from capsule
    capsule_id: str
    run_id: str
    asset_id: str | None = None
    schema_version: str = "1.0.0"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Completeness
    completeness: str = "partial"
    missing_fields: list[str] = Field(default_factory=list)


class RoPADocument(BaseModel):
    """Container for one or more RoPA entries (Art.30 register)."""
    controller_name: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "1.0.0"
    entries: list[RoPAEntry] = Field(default_factory=list)
    total_entries: int = 0
    completeness_summary: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------

class GDPRRoPAExporter:
    """Derives a GDPR Art.30 RoPA entry from a capsule directory.

    Fields directly derivable from the capsule are populated automatically.
    Fields requiring operator configuration (controller name, DPO contact,
    third-country transfer details) are populated from *operator_config* or
    left as stubs with an explicit ``missing_fields`` marker.
    """

    def __init__(self, operator_config: dict[str, Any] | None = None) -> None:
        self._cfg = operator_config or {}

    def build_ropa_entry(self, capsule_dir: Path) -> RoPAEntry:
        """Build a single RoPA entry from *capsule_dir*.

        Args:
            capsule_dir: Path to an unpacked capsule directory containing
                ``capsule.yaml``.

        Returns:
            A populated :class:`RoPAEntry`.
        """
        manifest = self._load_manifest(capsule_dir)

        run_id = manifest.get("run_id", "unknown")
        asset_id = manifest.get("asset_id")

        # Derive personal data categories from redaction manifest if present.
        pdc = self._derive_personal_data_categories(capsule_dir)

        # Security measures: always include NovaSeal envelope signing.
        security: list[str] = ["dsse_envelope_signing"]
        if (capsule_dir / "evidence_bundle.json").exists():
            security.append("evidence_bundle_integrity")
        if (capsule_dir / "seal.json").exists():
            security.append("rfc3161_timestamp")

        # Identify missing fields.
        missing: list[str] = []
        controller_name = self._cfg.get("controller_name", "")
        controller_contact = self._cfg.get("controller_contact", "")
        if not controller_name:
            missing.append("controller_name")
            controller_name = "OPERATOR_DECLARED_REQUIRED"
        if not controller_contact:
            missing.append("controller_contact")
            controller_contact = "OPERATOR_DECLARED_REQUIRED"

        third_country = [
            RoPATransfer(**t) for t in self._cfg.get("third_country_transfers", [])
        ]

        completeness = "complete" if not missing else "partial"

        return RoPAEntry(
            controller_name=controller_name,
            controller_contact=controller_contact,
            processing_purposes=self._cfg.get(
                "processing_purposes",
                ["ai_agent_run_capture", "model_performance_monitoring"],
            ),
            legal_basis=self._cfg.get("legal_basis", "legitimate_interests"),
            data_subject_categories=self._cfg.get(
                "data_subject_categories", ["end_users_of_ai_system"]
            ),
            personal_data_categories=pdc,
            recipient_categories=self._cfg.get("recipient_categories", []),
            third_country_transfers=third_country,
            retention_period=self._cfg.get("retention_period", "until_capsule_deletion"),
            security_measures=security,
            capsule_id=str(capsule_dir.name),
            run_id=run_id,
            asset_id=asset_id,
            completeness=completeness,
            missing_fields=missing,
        )

    def build_ropa_document(
        self,
        capsule_dirs: list[Path],
    ) -> RoPADocument:
        """Build a full RoPA register from multiple capsule directories."""
        controller_name = self._cfg.get("controller_name", "OPERATOR_DECLARED_REQUIRED")
        entries = [self.build_ropa_entry(d) for d in capsule_dirs]
        summary: dict[str, int] = {"complete": 0, "partial": 0, "missing": 0}
        for e in entries:
            summary[e.completeness] = summary.get(e.completeness, 0) + 1

        return RoPADocument(
            controller_name=controller_name,
            entries=entries,
            total_entries=len(entries),
            completeness_summary=summary,
        )

    def export_json(self, document: RoPADocument, output_path: Path) -> Path:
        """Write the RoPA document as JSON-LD to *output_path*."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "@context": {
                "gdpr": "https://w3id.org/dpv/dpv-gdpr#",
                "nova": "https://novafabric.dev/schema/ropa/v1#",
            },
            "@type": "gdpr:RecordsOfProcessingActivities",
            "nova:generatedAt": document.generated_at.isoformat(),
            "nova:schemaVersion": document.schema_version,
            "nova:controllerName": document.controller_name,
            "nova:totalEntries": document.total_entries,
            "nova:completenessSummary": document.completeness_summary,
            "gdpr:hasProcessingActivity": [
                self._entry_to_jsonld(e) for e in document.entries
            ],
        }
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        return output_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_manifest(self, capsule_dir: Path) -> dict[str, Any]:
        manifest_path = capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return {}
        import yaml  # already in core deps

        with open(manifest_path) as fh:
            return yaml.safe_load(fh) or {}

    def _derive_personal_data_categories(self, capsule_dir: Path) -> list[str]:
        """Infer personal data categories from the redaction manifest.

        Capture writes the hyphenated RedactionManifest whose records live under
        ``entries[].detection_rule_id``; we also accept the legacy underscore
        filename and the legacy ``redacted_items[].pii_type`` shape.
        """
        redaction_path = capsule_dir / "redaction-manifest.json"
        if not redaction_path.exists():
            redaction_path = capsule_dir / "redaction_manifest.json"
        if not redaction_path.exists():
            return []
        try:
            data = json.loads(redaction_path.read_text())
            types: set[str] = set()
            for entry in data.get("entries", []):
                rule = entry.get("detection_rule_id", "")
                if rule:
                    types.add(rule.lower())
            for item in data.get("redacted_items", []):  # legacy shape
                pii_type = item.get("pii_type", "")
                if pii_type:
                    types.add(pii_type.lower())
            return sorted(types)
        except Exception:
            return []

    def _entry_to_jsonld(self, entry: RoPAEntry) -> dict[str, Any]:
        return {
            "@type": "gdpr:ProcessingActivity",
            "nova:capsuleId": entry.capsule_id,
            "nova:runId": entry.run_id,
            "gdpr:hasController": {
                "gdpr:hasName": entry.controller_name,
                "gdpr:hasContact": entry.controller_contact,
            },
            "gdpr:hasPurpose": entry.processing_purposes,
            "gdpr:hasLegalBasis": entry.legal_basis,
            "gdpr:hasDataSubjectCategory": entry.data_subject_categories,
            "gdpr:hasPersonalDataCategory": entry.personal_data_categories,
            "gdpr:hasRetentionPeriod": entry.retention_period,
            "gdpr:hasTechnicalMeasure": entry.security_measures,
            "nova:completeness": entry.completeness,
            "nova:missingFields": entry.missing_fields,
        }
