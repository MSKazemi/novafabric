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
"""NIS2 incident report exporter (cap-005).

Implements the three NIS2 reporting phases per Directive (EU) 2022/2555:
  Phase 1 — Initial notification (Art. 23(3)): within 24 h of detection.
  Phase 2 — Detailed report (Art. 23(4)): within 72 h.
  Phase 3 — Final report (Art. 23(5)): within 1 month.

Fields requiring cap-006 (cross-session lineage) are explicitly marked
``missing`` with reason ``requires_cap_006_cross_session_lineage``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING,
    COMPLETENESS_PARTIAL,
    CompletenessSummaryEntry,
    NIS2IncidentReport,
)
from .provenance import EvidenceSource, EvidenceSourceRef, build_capsule_ref, mark

# Fields that cannot be populated until cap-006 is available.
_CAP006_BLOCKED_FIELDS = {"initial_root_cause_path", "final_root_cause"}
_CAP006_REASON = "requires_cap_006_cross_session_lineage"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_tool_permission_events(capsule_dir: Path) -> list[dict[str, Any]]:
    """Load tool_permission_event records from the capsule directory."""
    events_path = capsule_dir / "tool-permission-events.jsonl"
    if not events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def _denied_actions(events: list[dict[str, Any]]) -> list[str]:
    """Extract human-readable summaries of denied tool calls."""
    actions: list[str] = []
    for ev in events:
        if ev.get("decision") == "denied":
            tool = ev.get("tool_name", "unknown_tool")
            policy = ev.get("policy_id", "unknown_policy")
            ts = ev.get("capsule_timestamp_utc", "")
            actions.append(
                f"DENIED tool={tool} policy={policy} at={ts}"
            )
    return actions


def _entry(
    *,
    field_name: str,
    status: str,
    reason: str,
    capsule_derived: set[str],
    capsule_ref: EvidenceSourceRef | None,
    unverifiable: bool = False,
) -> CompletenessSummaryEntry:
    """Build a provenance-marked completeness entry (ADR-0197).

    * cap-006-blocked (``unverifiable=True``) → ``unverifiable`` (I-2: attempted
      and could not complete, never downgraded to an assertion);
    * genuinely capsule-derived and present → ``capsule_verified`` with a
      re-performable ref (I-3);
    * everything else → ``operator_asserted`` (I-1).
    """
    if unverifiable:
        source, ref = mark(EvidenceSource.unverifiable)
    elif field_name in capsule_derived and capsule_ref is not None:
        source, ref = mark(EvidenceSource.capsule_verified, ref=capsule_ref)
    else:
        source, ref = mark(EvidenceSource.operator_asserted)
    return CompletenessSummaryEntry(
        field_name=field_name,
        status=status,
        reason=reason,
        evidence_source=source,
        evidence_ref=ref,
    )


def _build_completeness(
    phase: int,
    report: dict[str, Any],
    cap006_available: bool,
    capsule_derived: set[str],
    capsule_ref: EvidenceSourceRef | None,
) -> list[CompletenessSummaryEntry]:
    """Build the provenance-marked completeness summary for the report."""
    entries: list[CompletenessSummaryEntry] = []

    # Phase 1 fields (always required)
    p1_fields = [
        "incident_id",
        "first_detection_timestamp",
        "preliminary_classification",
        "preliminary_severity_indicator",
        "cross_border_dimensions_flag",
    ]
    for f in p1_fields:
        val = report.get(f)
        status = COMPLETENESS_COMPLETE if val is not None else COMPLETENESS_MISSING
        present_str = "present" if status == COMPLETENESS_COMPLETE else "missing"
        entries.append(_entry(
            field_name=f,
            status=status,
            reason=f"Phase 1 required field — {present_str}",
            capsule_derived=capsule_derived,
            capsule_ref=capsule_ref,
        ))

    if phase >= 2:
        p2_fields = [
            "initial_root_cause_path",
            "initial_scope_estimate",
            "affected_systems_list",
            "actions_taken",
        ]
        for f in p2_fields:
            val = report.get(f)
            if f in _CAP006_BLOCKED_FIELDS and not cap006_available:
                entries.append(_entry(
                    field_name=f,
                    status=COMPLETENESS_MISSING,
                    reason=_CAP006_REASON,
                    capsule_derived=capsule_derived,
                    capsule_ref=capsule_ref,
                    unverifiable=True,
                ))
            elif val is not None:
                entries.append(_entry(
                    field_name=f,
                    status=COMPLETENESS_COMPLETE,
                    reason="Phase 2 field present",
                    capsule_derived=capsule_derived,
                    capsule_ref=capsule_ref,
                ))
            else:
                entries.append(_entry(
                    field_name=f,
                    status=COMPLETENESS_PARTIAL,
                    reason="Phase 2 optional field not populated",
                    capsule_derived=capsule_derived,
                    capsule_ref=capsule_ref,
                ))

    if phase >= 3:
        p3_fields = [
            "final_root_cause",
            "cross_border_impact_assessment",
            "lessons_learned",
        ]
        for f in p3_fields:
            if f in _CAP006_BLOCKED_FIELDS and not cap006_available:
                entries.append(_entry(
                    field_name=f,
                    status=COMPLETENESS_MISSING,
                    reason=_CAP006_REASON,
                    capsule_derived=capsule_derived,
                    capsule_ref=capsule_ref,
                    unverifiable=True,
                ))
            else:
                val = report.get(f)
                status = COMPLETENESS_COMPLETE if val else COMPLETENESS_PARTIAL
                present_str = "present" if status == COMPLETENESS_COMPLETE else "missing"
                entries.append(_entry(
                    field_name=f,
                    status=status,
                    reason=f"Phase 3 field — {present_str}",
                    capsule_derived=capsule_derived,
                    capsule_ref=capsule_ref,
                ))

    return entries


class NIS2Exporter:
    """Builds an :class:`NIS2IncidentReport` from a capsule directory.

    Phase 1 (within 24 h): uses the capsule manifest and tool permission events.
    Phase 2 (within 72 h): adds scope and action detail from tool_permission_events.
    Phase 3 (within 1 month): adds final root cause and lessons learned.

    Fields requiring cross-session lineage (cap-006) are explicitly marked
    ``missing`` with reason ``requires_cap_006_cross_session_lineage``.
    """

    def __init__(self, cap006_available: bool = False) -> None:
        """
        Args:
            cap006_available: Set to ``True`` once cap-006 (cross-session lineage)
                              is shipped.  Currently always ``False``.
        """
        self._cap006_available = cap006_available

    def build_nis2_report(
        self,
        incident_id: str,
        capsule_dir: Path,
        phase: int = 1,
        operator_inputs: dict[str, Any] | None = None,
    ) -> NIS2IncidentReport:
        """Build an :class:`NIS2IncidentReport` from capsule data.

        Args:
            incident_id: Operator-assigned incident identifier.
            capsule_dir: Path to the run capsule directory.
            phase: NIS2 reporting phase (1, 2, or 3).
            operator_inputs: Optional dict of operator-supplied field values
                             (e.g. ``{"preliminary_classification": "unauthorized_tool_use"}``).

        Returns:
            A :class:`NIS2IncidentReport` with completeness_summary populated.
        """
        if phase not in (1, 2, 3):
            raise ValueError(f"phase must be 1, 2, or 3 — got {phase}")

        manifest = self._load_manifest(capsule_dir)
        events = _read_tool_permission_events(capsule_dir)
        op = operator_inputs or {}

        generated_at = _now()
        capsule_ref = build_capsule_ref(capsule_dir, generated_at)
        # Track which field values genuinely originate from the capsule so only
        # those are marked capsule_verified (ADR-0197 I-1/I-4).
        capsule_derived: set[str] = set()

        # Phase 1 fields
        if "first_detection_timestamp" not in op and manifest.get("created_at"):
            capsule_derived.add("first_detection_timestamp")
        first_detection = op.get(
            "first_detection_timestamp",
            manifest.get("created_at", generated_at),
        )
        classification = op.get(
            "preliminary_classification",
            "unclassified",
        )
        severity = op.get("preliminary_severity_indicator", "medium")
        cross_border = bool(op.get("cross_border_dimensions_flag", False))

        # Phase 2 fields
        root_cause_path: list[str] | None = None  # cap-006 required
        scope_estimate: str | None = None
        affected_systems: list[str] | None = None
        actions_taken: list[str] | None = None

        if phase >= 2:
            scope_estimate = op.get("initial_scope_estimate")
            affected_systems = op.get("affected_systems_list")
            denied = _denied_actions(events)
            if denied:
                actions_taken = denied
                capsule_derived.add("actions_taken")  # from capsule events
            elif op.get("actions_taken"):
                actions_taken = op["actions_taken"]

        # Phase 3 fields
        final_root_cause: str | None = None  # cap-006 required
        cross_border_impact: str | None = None
        lessons_learned: str | None = None

        if phase >= 3:
            cross_border_impact = op.get("cross_border_impact_assessment")
            lessons_learned = op.get("lessons_learned")

        # Build partial dict for completeness assessment
        report_dict: dict[str, Any] = {
            "incident_id": incident_id,
            "first_detection_timestamp": first_detection,
            "preliminary_classification": classification,
            "preliminary_severity_indicator": severity,
            "cross_border_dimensions_flag": cross_border,
            "initial_root_cause_path": root_cause_path,
            "initial_scope_estimate": scope_estimate,
            "affected_systems_list": affected_systems,
            "actions_taken": actions_taken,
            "final_root_cause": final_root_cause,
            "cross_border_impact_assessment": cross_border_impact,
            "lessons_learned": lessons_learned,
        }

        completeness = _build_completeness(
            phase,
            report_dict,
            self._cap006_available,
            capsule_derived,
            capsule_ref,
        )

        return NIS2IncidentReport(
            incident_id=incident_id,
            first_detection_timestamp=first_detection,
            preliminary_classification=classification,
            preliminary_severity_indicator=severity,
            cross_border_dimensions_flag=cross_border,
            initial_root_cause_path=root_cause_path,
            initial_scope_estimate=scope_estimate,
            affected_systems_list=affected_systems,
            actions_taken=actions_taken,
            final_root_cause=final_root_cause,
            cross_border_impact_assessment=cross_border_impact,
            lessons_learned=lessons_learned,
            completeness_summary=completeness,
            generated_at=generated_at,
            report_phase=phase,
        )

    def _load_manifest(self, capsule_dir: Path) -> dict[str, Any]:
        manifest_path = capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return {}
        with open(manifest_path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
