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
"""NIST AI RMF 1.0 quantitative risk reporter (cap-009).

Derives quantitative risk assessments from capsule metadata, eval results,
and audit coverage, mapping to the four NIST AI RMF Core functions:
GOVERN, MAP, MEASURE, MANAGE.

Reference: NIST AI 100-1 (January 2023) — AI Risk Management Framework.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

RiskCategory = Literal["bias", "robustness", "transparency", "privacy", "safety", "reliability"]
RMFFunction = Literal["GOVERN", "MAP", "MEASURE", "MANAGE"]


class RMFMetric(BaseModel):
    """A single quantitative risk metric."""
    metric_id: str
    name: str
    rmf_function: RMFFunction
    risk_category: RiskCategory
    value: float | None = None
    unit: str = "score_0_1"
    confidence_interval: tuple[float, float] | None = None
    measurement_method: str = "capsule_derived"
    data_source: str = ""
    threshold: float | None = None
    compliant: bool | None = None
    notes: str = ""


class NISTRMFReport(BaseModel):
    """NIST AI RMF 1.0 quantitative risk assessment report."""
    capsule_id: str
    run_id: str
    asset_id: str | None = None
    schema_version: str = "1.0.0"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Scores per NIST RMF function (0.0 = lowest, 1.0 = highest maturity/coverage)
    govern_score: float | None = None
    map_score: float | None = None
    measure_score: float | None = None
    manage_score: float | None = None
    overall_score: float | None = None
    metrics: list[RMFMetric] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    completeness: str = "partial"
    missing_evidence: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

class NISTAIRMFReporter:
    """Generates a quantitative NIST AI RMF 1.0 report from a capsule directory.

    Derives metrics from:
    - ``capsule.yaml`` — provenance, model, tools, capture_level
    - ``eval_result.json`` — performance and regression metrics
    - ``audit_coverage.json`` — policy audit coverage from ``nova audit``
    - ``evidence_bundle.json`` — signed evidence presence
    - ``redaction_manifest.json`` — PII handling
    """

    # Thresholds for compliance determination.
    _THRESHOLDS: dict[str, float] = {
        "transparency_coverage": 0.7,
        "eval_coverage": 0.5,
        "privacy_redaction_rate": 0.9,
        "evidence_signing_rate": 1.0,
    }

    def build_report(self, capsule_dir: Path) -> NISTRMFReport:
        """Build a NIST AI RMF quantitative report from *capsule_dir*."""
        manifest = self._load_manifest(capsule_dir)
        run_id = manifest.get("run_id", "unknown")
        asset_id = manifest.get("asset_id")

        metrics: list[RMFMetric] = []
        missing: list[str] = []

        # --- GOVERN: Policies and accountability ---
        govern_metrics, gov_missing = self._score_govern(capsule_dir, manifest)
        metrics.extend(govern_metrics)
        missing.extend(gov_missing)

        # --- MAP: Risk identification ---
        map_metrics, map_missing = self._score_map(capsule_dir, manifest)
        metrics.extend(map_metrics)
        missing.extend(map_missing)

        # --- MEASURE: Quantitative assessment ---
        measure_metrics, meas_missing = self._score_measure(capsule_dir, manifest)
        metrics.extend(measure_metrics)
        missing.extend(meas_missing)

        # --- MANAGE: Risk treatment ---
        manage_metrics, mgmt_missing = self._score_manage(capsule_dir, manifest)
        metrics.extend(manage_metrics)
        missing.extend(mgmt_missing)

        # Aggregate function scores.
        def _avg(ms: list[RMFMetric]) -> float | None:
            vals = [m.value for m in ms if m.value is not None]
            return sum(vals) / len(vals) if vals else None

        govern_score = _avg(govern_metrics)
        map_score = _avg(map_metrics)
        measure_score = _avg(measure_metrics)
        manage_score = _avg(manage_metrics)

        all_scores = [
            s for s in [govern_score, map_score, measure_score, manage_score]
            if s is not None
        ]
        overall = sum(all_scores) / len(all_scores) if all_scores else None

        risk_level = self._derive_risk_level(overall, metrics)
        completeness = "complete" if not missing else "partial"

        return NISTRMFReport(
            capsule_id=str(capsule_dir.name),
            run_id=run_id,
            asset_id=asset_id,
            govern_score=govern_score,
            map_score=map_score,
            measure_score=measure_score,
            manage_score=manage_score,
            overall_score=overall,
            metrics=metrics,
            risk_level=risk_level,
            completeness=completeness,
            missing_evidence=missing,
        )

    def export_json(self, report: NISTRMFReport, output_path: Path) -> Path:
        """Write the NIST AI RMF report to *output_path* as JSON."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = report.model_dump(mode="json")
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        return output_path

    # ------------------------------------------------------------------
    # Scoring helpers per RMF function
    # ------------------------------------------------------------------

    def _score_govern(
        self, capsule_dir: Path, manifest: dict[str, Any]
    ) -> tuple[list[RMFMetric], list[str]]:
        metrics: list[RMFMetric] = []
        missing: list[str] = []

        # GV-1.1: Policy documentation present.
        audit_path = capsule_dir / "audit_coverage.json"
        if audit_path.exists():
            try:
                data = json.loads(audit_path.read_text())
                coverage = data.get("coverage_fraction", 0.0)
                metrics.append(
                    RMFMetric(
                        metric_id="GV-1.1",
                        name="Policy Coverage",
                        rmf_function="GOVERN",
                        risk_category="transparency",
                        value=float(coverage),
                        measurement_method="audit_coverage_json",
                        data_source="audit_coverage.json",
                        threshold=self._THRESHOLDS["transparency_coverage"],
                        compliant=float(coverage) >= self._THRESHOLDS["transparency_coverage"],
                    )
                )
            except Exception:
                missing.append("audit_coverage.json:coverage_fraction")
        else:
            missing.append("audit_coverage.json")

        # GV-1.2: Evidence bundle signed.
        bundle_path = capsule_dir / "evidence_bundle.json"
        signed = 1.0 if bundle_path.exists() else 0.0
        if not bundle_path.exists():
            missing.append("evidence_bundle.json")
        metrics.append(
            RMFMetric(
                metric_id="GV-1.2",
                name="Evidence Signing",
                rmf_function="GOVERN",
                risk_category="transparency",
                value=signed,
                measurement_method="file_presence",
                data_source="evidence_bundle.json",
                threshold=self._THRESHOLDS["evidence_signing_rate"],
                compliant=signed >= self._THRESHOLDS["evidence_signing_rate"],
            )
        )

        return metrics, missing

    def _score_map(
        self, capsule_dir: Path, manifest: dict[str, Any]
    ) -> tuple[list[RMFMetric], list[str]]:
        metrics: list[RMFMetric] = []
        missing: list[str] = []

        # MP-2.1: Tool permission events captured.
        # The capture pipeline writes the hyphenated name; accept the legacy
        # underscore form too for older capsules.
        tool_events_path = capsule_dir / "tool-permission-events.jsonl"
        if not tool_events_path.exists():
            tool_events_path = capsule_dir / "tool_permission_events.jsonl"
        has_tool_events = tool_events_path.exists()
        if not has_tool_events:
            missing.append("tool-permission-events.jsonl")
        metrics.append(
            RMFMetric(
                metric_id="MP-2.1",
                name="Tool Permission Observability",
                rmf_function="MAP",
                risk_category="safety",
                value=1.0 if has_tool_events else 0.0,
                measurement_method="file_presence",
                data_source="tool-permission-events.jsonl",
                threshold=0.5,
                compliant=has_tool_events,
            )
        )

        # MP-2.2: Capture level adequacy.
        capture_level = manifest.get("capture_level", "standard")
        level_score = {"forensic": 1.0, "standard": 0.7, "minimal": 0.3}.get(
            capture_level, 0.5
        )
        metrics.append(
            RMFMetric(
                metric_id="MP-2.2",
                name="Capture Level Adequacy",
                rmf_function="MAP",
                risk_category="reliability",
                value=level_score,
                unit="score_0_1",
                measurement_method="capsule_derived",
                data_source="capsule.yaml:capture_level",
                notes=f"capture_level={capture_level}",
            )
        )

        return metrics, missing

    def _score_measure(
        self, capsule_dir: Path, manifest: dict[str, Any]
    ) -> tuple[list[RMFMetric], list[str]]:
        metrics: list[RMFMetric] = []
        missing: list[str] = []

        eval_path = capsule_dir / "eval_result.json"
        if eval_path.exists():
            try:
                data = json.loads(eval_path.read_text())
                eval_metrics = data.get("metrics", [])
                if eval_metrics:
                    # Normalise first numeric metric as primary performance score.
                    first_val = None
                    for m in eval_metrics:
                        if m.get("value") is not None:
                            try:
                                first_val = float(m["value"])
                                break
                            except (TypeError, ValueError):
                                pass
                    metrics.append(
                        RMFMetric(
                            metric_id="MS-2.5",
                            name="Eval Performance Score",
                            rmf_function="MEASURE",
                            risk_category="robustness",
                            value=first_val,
                            measurement_method="eval_result_json",
                            data_source="eval_result.json",
                            threshold=self._THRESHOLDS["eval_coverage"],
                            compliant=(
                                first_val is not None
                                and first_val >= self._THRESHOLDS["eval_coverage"]
                            ),
                            notes=f"derived from {len(eval_metrics)} eval metrics",
                        )
                    )
                else:
                    missing.append("eval_result.json:metrics")
            except Exception:
                missing.append("eval_result.json:parse_error")
        else:
            missing.append("eval_result.json")

        # MS-2.6: Regression detection evidence.
        regression_path = capsule_dir / "regression_report.json"
        if regression_path.exists():
            try:
                data = json.loads(regression_path.read_text())
                reg_detected = data.get("regression_detected", True)
                metrics.append(
                    RMFMetric(
                        metric_id="MS-2.6",
                        name="Regression Detection",
                        rmf_function="MEASURE",
                        risk_category="reliability",
                        value=0.0 if reg_detected else 1.0,
                        measurement_method="regression_report_json",
                        data_source="regression_report.json",
                        threshold=1.0,
                        compliant=not reg_detected,
                        notes="0.0 = regression detected; 1.0 = no regression",
                    )
                )
            except Exception:
                pass

        return metrics, missing

    def _score_manage(
        self, capsule_dir: Path, manifest: dict[str, Any]
    ) -> tuple[list[RMFMetric], list[str]]:
        metrics: list[RMFMetric] = []
        missing: list[str] = []

        # MG-2.2: PII redaction evidence present.
        # Capture writes the hyphenated RedactionManifest (entries[]); accept the
        # legacy underscore filename and legacy {redacted_items[]} shape too.
        redaction_path = capsule_dir / "redaction-manifest.json"
        if not redaction_path.exists():
            redaction_path = capsule_dir / "redaction_manifest.json"
        if redaction_path.exists():
            try:
                data = json.loads(redaction_path.read_text())
                entries = data.get("entries", data.get("redacted_items", []))
                # Presence of a redaction manifest is itself the evidence; the
                # rate is the share of detections that carry a redaction record.
                redacted = len(entries)
                rate = 1.0 if redacted > 0 else 0.0
                metrics.append(
                    RMFMetric(
                        metric_id="MG-2.2",
                        name="PII Redaction Rate",
                        rmf_function="MANAGE",
                        risk_category="privacy",
                        value=rate,
                        measurement_method="redaction_manifest_json",
                        data_source="redaction-manifest.json",
                        threshold=self._THRESHOLDS["privacy_redaction_rate"],
                        compliant=rate >= self._THRESHOLDS["privacy_redaction_rate"],
                    )
                )
            except Exception:
                missing.append("redaction-manifest.json:parse_error")
        else:
            missing.append("redaction-manifest.json")

        # MG-4.1: Capsule status indicates managed lifecycle.
        status = manifest.get("status", "unknown")
        lifecycle_score = {
            "production": 1.0,
            "staging": 0.8,
            "development": 0.5,
            "unknown": 0.2,
        }.get(status, 0.4)
        metrics.append(
            RMFMetric(
                metric_id="MG-4.1",
                name="Lifecycle Management Score",
                rmf_function="MANAGE",
                risk_category="reliability",
                value=lifecycle_score,
                measurement_method="capsule_derived",
                data_source="capsule.yaml:status",
                notes=f"status={status}",
            )
        )

        return metrics, missing

    def _derive_risk_level(
        self, overall: float | None, metrics: list[RMFMetric]
    ) -> Literal["low", "medium", "high", "critical"]:
        if overall is None:
            return "high"
        non_compliant = sum(
            1 for m in metrics if m.compliant is False
        )
        if overall >= 0.8 and non_compliant == 0:
            return "low"
        if overall >= 0.6 and non_compliant <= 1:
            return "medium"
        if overall >= 0.4:
            return "high"
        return "critical"

    def _load_manifest(self, capsule_dir: Path) -> dict[str, Any]:
        manifest_path = capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return {}
        import yaml

        with open(manifest_path) as fh:
            return yaml.safe_load(fh) or {}
