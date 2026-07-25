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
"""EU AI Act Article 12 automatic-logging conformance exporter (ADR-0107 NF-090).

Article 12 of Regulation (EU) 2024/1689 requires high-risk AI systems to
*technically allow for the automatic recording of events (logs)* over their
operation, ensuring traceability appropriate to the intended purpose. This is a
**render-from-sealed-facts** exporter (same pattern as the Annex IV / NIS2
exporters): it reads a run capsule's manifest, maps the evidence NovaFabric
already captured to each Art. 12 record-keeping requirement, and marks it
`complete` / `partial` / `missing` — each with an ADR-0197 `evidence_source`, so a
consumer can tell a capsule-verified fact from an operator assertion.

It renders evidence that **supports** an Art. 12 assessment; it does not certify
conformity (no verdict field). It runs offline against a local capsule — no server
mode, no network (ADR-0107 R-5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .models import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING,
    CompletenessSummaryEntry,
)
from .provenance import EvidenceSource, build_capsule_ref, mark

ART12_STANDARD = "EU AI Act Art. 12 — Record-keeping (Regulation (EU) 2024/1689)"

#: (requirement_key, human description). Order is stable for deterministic output.
ART12_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("automatic_event_recording",
     "Art. 12(1) — automatic recording of events (logs) over the system's operation"),
    ("period_of_use",
     "Art. 12(2)(a) — recording of the start and end of each period of use"),
    ("traceability",
     "Art. 12(1) — traceability of the system's functioning appropriate to intended purpose"),
    ("input_output_records",
     "Art. 12 — records of inputs/outputs enabling verification of results"),
    ("tamper_evidence",
     "Art. 12 — logs protected against tampering (integrity of the record)"),
    ("retention",
     "Art. 12(3) — logs retained for a period appropriate to the intended purpose"),
)


class Art12ConformanceReport(BaseModel):
    """An EU AI Act Art. 12 record-keeping conformance evidence report."""

    standard: str = Field(default=ART12_STANDARD)
    deployment_id: str
    generated_at: str
    completeness_summary: list[CompletenessSummaryEntry]
    disclaimer: str = Field(
        default=(
            "Renders evidence that supports an Art. 12 record-keeping assessment; "
            "it does not certify conformity. A qualified human makes the determination."
        )
    )
    # Intentionally NO conforming/verdict field — renders facts, never certifies.


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _entry(
    field_name: str,
    reason: str,
    *,
    complete: bool,
    source: EvidenceSource,
    ref: Any = None,
) -> CompletenessSummaryEntry:
    if source is EvidenceSource.capsule_verified:
        src, marked_ref = mark(source, ref=ref)
    else:
        src, marked_ref = mark(source)
    return CompletenessSummaryEntry(
        field_name=field_name,
        status=COMPLETENESS_COMPLETE if complete else COMPLETENESS_MISSING,
        reason=reason,
        evidence_source=src,
        evidence_ref=marked_ref,
    )


def _cv_or_unv(present: bool, capsule_ref: Any) -> EvidenceSource:
    """capsule_verified when present with a re-performable ref, else unverifiable."""
    if present and capsule_ref is not None:
        return EvidenceSource.capsule_verified
    return EvidenceSource.unverifiable


def build_art12_report(
    capsule_dir: Path,
    *,
    deployment_id: str,
    retention_days: int | None = None,
) -> Art12ConformanceReport:
    """Build an Art. 12 conformance report from a run capsule.

    Args:
        capsule_dir: path to the run capsule directory (must contain ``capsule.yaml``).
        deployment_id: operator-assigned identifier for the AI system deployment.
        retention_days: operator-declared log-retention period, if any.

    Raises:
        FileNotFoundError: if ``capsule.yaml`` is absent.
    """
    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"capsule.yaml not found in {capsule_dir}")
    manifest: dict[str, Any] = yaml.safe_load(manifest_path.read_text()) or {}

    generated_at = _now()
    capsule_ref = build_capsule_ref(capsule_dir, generated_at)

    def _has_stream(*names: str) -> bool:
        return any((capsule_dir / n).exists() for n in names)

    entries: list[CompletenessSummaryEntry] = []

    # Automatic event recording — the capsule's event streams are the record.
    has_events = _has_stream("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl")
    if has_events and capsule_ref is not None:
        entries.append(_entry(
            "automatic_event_recording",
            "capsule carries automatically-recorded event streams (model/tool calls, trace)",
            complete=True, source=EvidenceSource.capsule_verified, ref=capsule_ref,
        ))
    else:
        entries.append(_entry(
            "automatic_event_recording",
            "no automatically-recorded event streams found in the capsule",
            complete=False, source=EvidenceSource.unverifiable,
        ))

    # Period of use — start/end timestamps from the manifest.
    has_period = bool(manifest.get("created_at")) and bool(manifest.get("finished_at"))
    entries.append(_entry(
        "period_of_use",
        "capsule manifest records created_at and finished_at (start/end of use)"
        if has_period else "capsule manifest is missing created_at/finished_at",
        complete=has_period,
        source=_cv_or_unv(has_period, capsule_ref),
        ref=capsule_ref,
    ))

    # Traceability — a trace stream / trace_ref.
    has_trace = bool(manifest.get("trace_ref")) or _has_stream("trace.jsonl")
    entries.append(_entry(
        "traceability",
        "capsule carries an execution trace enabling traceability"
        if has_trace else "no execution trace found for traceability",
        complete=has_trace,
        source=_cv_or_unv(has_trace, capsule_ref),
        ref=capsule_ref,
    ))

    # Input/output records.
    has_io = _has_stream("model-calls.jsonl") or bool(manifest.get("model_calls_ref"))
    entries.append(_entry(
        "input_output_records",
        "capsule records model inputs/outputs enabling verification of results"
        if has_io else "no input/output records found",
        complete=has_io,
        source=_cv_or_unv(has_io, capsule_ref),
        ref=capsule_ref,
    ))

    # Tamper-evidence — a NovaSeal envelope / signature.
    has_seal = _has_stream("seal-envelope.json", "novaseal-envelope.json") or bool(
        manifest.get("seal_ref")
    )
    if has_seal and capsule_ref is not None:
        entries.append(_entry(
            "tamper_evidence",
            "capsule is sealed (NovaSeal envelope present); logs are tamper-evident",
            complete=True, source=EvidenceSource.capsule_verified, ref=capsule_ref,
        ))
    else:
        entries.append(_entry(
            "tamper_evidence",
            "no seal envelope found; log tamper-evidence could not be verified",
            complete=False, source=EvidenceSource.unverifiable,
        ))

    # Retention — operator-declared policy.
    if retention_days is not None:
        entries.append(_entry(
            "retention",
            f"operator-declared log retention of {retention_days} days",
            complete=True, source=EvidenceSource.operator_asserted,
        ))
    else:
        entries.append(_entry(
            "retention",
            "no operator-declared log-retention period",
            complete=False, source=EvidenceSource.operator_asserted,
        ))

    return Art12ConformanceReport(
        deployment_id=deployment_id,
        generated_at=generated_at,
        completeness_summary=entries,
    )


__all__ = [
    "ART12_REQUIREMENTS",
    "ART12_STANDARD",
    "Art12ConformanceReport",
    "build_art12_report",
]
