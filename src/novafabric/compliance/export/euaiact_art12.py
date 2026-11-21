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

Article 12(1) of Regulation (EU) 2024/1689 requires high-risk AI systems to
*technically allow for the automatic recording of events (logs) over the lifetime
of the system*. Art. 12(2) sets what logging is for: a level of traceability
appropriate to the intended purpose, through events relevant for (a) risk
situations and substantial modification, (b) post-market monitoring and (c)
monitoring the system's operation. Art. 12(3) adds minimum records for remote
biometric identification systems (Annex III point 1(a)). The report also covers
the log provisions outside Art. 12 that an assessment needs: the instructions for
use (Art. 13(3)(f)), log retention (Art. 19(1) for providers, Art. 26(6) for
deployers) and authority access (Art. 21(2)).

This is a **render-from-sealed-facts** exporter (same pattern as the Annex IV /
NIS2 exporters): it reads a run capsule's manifest, maps the evidence NovaFabric
already captured to each requirement, and marks it `complete` / `partial` /
`missing` — each with an ADR-0197 `evidence_source`, so a consumer can tell a
capsule-verified fact from an operator assertion.

Each row also has a ``basis`` (see :class:`Art12Requirement`): an AI Act duty; a
standards item that goes beyond the Act's text (log tamper-evidence — the Act
sets no such duty); or NovaFabric evidence that supports Act duties without being
one itself (input/output records).

It renders evidence that **supports** an Art. 12 assessment; it does not certify
conformity (no verdict field). It runs offline against a local capsule — no server
mode, no network (ADR-0107 R-5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from novafabric._capsule_fs import read_required_capsule_member

from ._seal_guard import seal_for_log_claims
from .models import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING,
    COMPLETENESS_PARTIAL,
    CompletenessSummaryEntry,
)
from .provenance import EvidenceSource, EvidenceSourceRef, build_capsule_ref, mark

ART12_STANDARD = (
    "EU AI Act Art. 12 — Record-keeping, with the related log provisions of "
    "Arts. 13(3)(f), 19(1), 21(2) and 26(6) (Regulation (EU) 2024/1689)"
)

#: Where a row's requirement comes from.
#: ``ai_act`` — a paragraph of Regulation (EU) 2024/1689.
#: ``standards`` — a logging-standard topic that goes beyond the Act's text.
#: ``novafabric_evidence`` — evidence NovaFabric records in support of Act duties,
#: not an Act duty itself.
RequirementBasis = Literal["ai_act", "standards", "novafabric_evidence"]

#: (requirement_key, human description) for the rows every report carries. Order
#: is stable for deterministic output; the first six keys are the original rows
#: and keep their positions.
ART12_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("automatic_event_recording",
     "Art. 12(1) — the system technically allows the automatic recording of events "
     "(logs) over its lifetime"),
    ("period_of_use",
     "Art. 12(3)(a) — recording of the period of each use (start and end date and "
     "time); required for remote biometric identification systems (Annex III point "
     "1(a)), reported for every run"),
    ("traceability",
     "Art. 12(2), chapeau — logging ensures a level of traceability of the system's "
     "functioning appropriate to its intended purpose"),
    ("input_output_records",
     "NovaFabric evidence, not an Act duty of its own — recorded model inputs and "
     "outputs, supporting the Art. 12(2)(a)-(c) purposes (for remote biometric "
     "identification see also Art. 12(3)(c))"),
    ("tamper_evidence",
     "Standards item, beyond the Act's text — log integrity protection "
     "(prEN 18229-1; ISO/IEC 24970). The AI Act sets no log tamper-protection duty"),
    ("retention",
     "Art. 19(1) (providers) / Art. 26(6) (deployers) — automatically generated logs "
     "under the party's control kept for a period appropriate to the intended "
     "purpose, at least six months unless Union or national law provides otherwise"),
    ("risk_situation_events",
     "Art. 12(2)(a) — logging enables recording of events relevant for identifying "
     "situations that may present a risk within the meaning of Art. 79(1) or lead "
     "to a substantial modification"),
    ("post_market_monitoring_events",
     "Art. 12(2)(b) — logging enables recording of events relevant for facilitating "
     "post-market monitoring (Art. 72)"),
    ("deployer_monitoring_events",
     "Art. 12(2)(c) — logging enables recording of events relevant for monitoring "
     "the system's operation (Art. 26(5))"),
    ("log_instructions_for_use",
     "Art. 13(3)(f) — the instructions for use describe, where relevant, the "
     "mechanisms that let deployers properly collect, store and interpret the logs"),
    ("authority_log_access",
     "Art. 21(2) — on a reasoned request, the provider gives the competent authority "
     "access to the automatically generated logs under its control"),
)

#: Rows that apply only to remote biometric identification systems (Annex III
#: point 1(a)). A report carries them only with ``remote_biometric_identification``.
ART12_RBI_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("rbi_reference_database",
     "Art. 12(3)(b) — the reference database against which input data has been "
     "checked (remote biometric identification only)"),
    ("rbi_matched_input_data",
     "Art. 12(3)(c) — the input data for which the search has led to a match "
     "(remote biometric identification only)"),
    ("rbi_verifying_persons",
     "Art. 12(3)(d) — identification of the natural persons involved in verifying "
     "the results, as referred to in Art. 14(5) (remote biometric identification "
     "only)"),
)

#: The basis of each row that is not an AI Act duty; every other row is ``ai_act``.
ART12_NON_ACT_BASIS: dict[str, RequirementBasis] = {
    "input_output_records": "novafabric_evidence",
    "tamper_evidence": "standards",
}


def art12_requirements(
    *, remote_biometric_identification: bool = False,
) -> tuple[tuple[str, str], ...]:
    """The (key, label) rows a report carries; RBI-only rows only when they apply."""
    if remote_biometric_identification:
        return ART12_REQUIREMENTS + ART12_RBI_REQUIREMENTS
    return ART12_REQUIREMENTS


class Art12Requirement(BaseModel):
    """What one report row checks, and where that requirement comes from."""

    field_name: str
    label: str
    basis: RequirementBasis


class Art12ConformanceReport(BaseModel):
    """An EU AI Act Art. 12 record-keeping conformance evidence report."""

    standard: str = Field(default=ART12_STANDARD)
    deployment_id: str
    generated_at: str
    completeness_summary: list[CompletenessSummaryEntry]
    requirements: list[Art12Requirement] = Field(
        default_factory=list,
        description=(
            "One entry per completeness_summary row: the provision it cites and "
            "whether that is an AI Act duty, a standards item or NovaFabric evidence."
        ),
    )
    remote_biometric_identification: bool = Field(
        default=False,
        description=(
            "Whether the operator declared an Annex III point 1(a) remote biometric "
            "identification system, which adds the Art. 12(3)(b)-(d) rows."
        ),
    )
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
    partial: bool = False,
) -> CompletenessSummaryEntry:
    if source is EvidenceSource.capsule_verified:
        src, marked_ref = mark(source, ref=ref)
    else:
        src, marked_ref = mark(source)
    if partial:
        status = COMPLETENESS_PARTIAL
    else:
        status = COMPLETENESS_COMPLETE if complete else COMPLETENESS_MISSING
    return CompletenessSummaryEntry(
        field_name=field_name,
        status=status,
        reason=reason,
        evidence_source=src,
        evidence_ref=marked_ref,
    )


def _cv_or_unv(present: bool, capsule_ref: Any) -> EvidenceSource:
    """capsule_verified when present with a re-performable ref, else unverifiable."""
    if present and capsule_ref is not None:
        return EvidenceSource.capsule_verified
    return EvidenceSource.unverifiable


def _purpose_entry(
    field_name: str,
    purpose: str,
    *,
    has_events: bool,
    capsule_ref: EvidenceSourceRef | None,
) -> CompletenessSummaryEntry:
    """An Art. 12(2)(a)-(c) row: the events exist; their relevance is not checked."""
    if has_events and capsule_ref is not None:
        return _entry(
            field_name,
            f"capsule carries automatically-recorded events (model/tool calls, trace) "
            f"that can serve {purpose}; which events are relevant is set by the "
            "provider's risk management and is not checked here",
            complete=False, partial=True,
            source=EvidenceSource.capsule_verified, ref=capsule_ref,
        )
    return _entry(
        field_name,
        f"no automatically-recorded event streams found to serve {purpose}",
        complete=False, source=EvidenceSource.unverifiable,
    )


def build_art12_report(
    capsule_dir: Path,
    *,
    deployment_id: str,
    retention_days: int | None = None,
    log_instructions_ref: str | None = None,
    remote_biometric_identification: bool = False,
) -> Art12ConformanceReport:
    """Build an Art. 12 conformance report from a run capsule.

    Args:
        capsule_dir: path to the run capsule directory (must contain ``capsule.yaml``).
        deployment_id: operator-assigned identifier for the AI system deployment.
        retention_days: operator-declared log-retention period, if any. Art. 19(1)
            and Art. 26(6) set a floor of six months; the value is reported as
            declared, not compared against that floor.
        log_instructions_ref: operator-declared reference (document, section, URL)
            to the instructions-for-use text that describes how deployers collect,
            store and interpret the logs (Art. 13(3)(f)).
        remote_biometric_identification: the system is a remote biometric
            identification system (Annex III point 1(a)); adds the
            Art. 12(3)(b)-(d) rows.

    Raises:
        FileNotFoundError: if ``capsule.yaml`` is absent.
        CapsuleRequiredFileError: ``capsule.yaml`` is present but unusable (a
            link, FIFO, directory). Never an Art.12 record with empty fields.
        CapsuleSealInvalidError: the capsule carries a ``.seal`` that fails
            verification (edited after sealing, or a damaged or substituted seal).
            Refused rather than rendered from bytes the seal contradicts. A capsule
            that was never sealed, or whose valid seal does not bind the evidence
            streams, is still reported, as not tamper-evident.
    """
    capsule_text = read_required_capsule_member(
        capsule_dir / "capsule.yaml", operation="compliance export"
    )
    if capsule_text is None:
        raise FileNotFoundError(f"capsule.yaml not found in {capsule_dir}")
    manifest: dict[str, Any] = yaml.safe_load(capsule_text) or {}
    # Tri-state, not capsule_seal_verifies: a seal that exists but does not verify is
    # evidence of tampering, so no report is built from the edited bytes.
    seal_state = seal_for_log_claims(capsule_dir)

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

    # Input/output records — NovaFabric evidence, not a stand-alone Act duty.
    has_io = _has_stream("model-calls.jsonl") or bool(manifest.get("model_calls_ref"))
    entries.append(_entry(
        "input_output_records",
        "capsule records model inputs/outputs enabling verification of results"
        if has_io else "no input/output records found",
        complete=has_io,
        source=_cv_or_unv(has_io, capsule_ref),
        ref=capsule_ref,
    ))

    # Tamper-evidence — a standards item; a NovaSeal seal that verifies and binds the
    # evidence streams (review compliance-privacy-12): this read file names NovaFabric
    # never writes, so a hand-made file counted and a genuine `.seal/` read as unsealed.
    sealed = seal_state == "bound"
    if sealed and capsule_ref is not None:
        entries.append(_entry(
            "tamper_evidence",
            "the capsule's NovaSeal seal verifies against its files; a change to a sealed "
            "log is detectable (the signer is not anchored to a trust list by this check)",
            complete=True, source=EvidenceSource.capsule_verified, ref=capsule_ref,
        ))
    elif seal_state == "unbound":
        # The seal verifies, but it cannot tell an untouched stream from an edited or
        # injected one, so it establishes nothing about the logs. Not "no seal".
        entries.append(_entry(
            "tamper_evidence",
            "a NovaSeal seal verifies but does not bind the evidence streams (it signs the "
            "manifest only, or a stream was added after sealing); log tamper-evidence is "
            "not established",
            complete=False, source=EvidenceSource.unverifiable,
        ))
    else:
        entries.append(_entry(
            "tamper_evidence",
            "no NovaSeal seal that verifies; log tamper-evidence is not established",
            complete=False, source=EvidenceSource.unverifiable,
        ))

    # Retention — operator-declared policy (Art. 19(1) / Art. 26(6)).
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

    # Art. 12(2)(a)-(c) — the purposes logging must serve.
    entries.append(_purpose_entry(
        "risk_situation_events",
        "identifying risk situations (Art. 79(1)) and substantial modifications",
        has_events=has_events, capsule_ref=capsule_ref,
    ))
    entries.append(_purpose_entry(
        "post_market_monitoring_events",
        "post-market monitoring (Art. 72)",
        has_events=has_events, capsule_ref=capsule_ref,
    ))
    entries.append(_purpose_entry(
        "deployer_monitoring_events",
        "monitoring the system's operation (Art. 26(5))",
        has_events=has_events, capsule_ref=capsule_ref,
    ))

    # Art. 13(3)(f) — the instructions for use; an operator document.
    if log_instructions_ref:
        entries.append(_entry(
            "log_instructions_for_use",
            "operator-declared instructions-for-use description of log collection, "
            f"storage and interpretation: {log_instructions_ref}",
            complete=True, source=EvidenceSource.operator_asserted,
        ))
    else:
        entries.append(_entry(
            "log_instructions_for_use",
            "no operator-declared instructions-for-use description of how deployers "
            "collect, store and interpret the logs",
            complete=False, source=EvidenceSource.operator_asserted,
        ))

    # Art. 21(2) — the capsule is a log an authority can be given and check offline.
    if has_events and capsule_ref is not None:
        if sealed:
            check = (
                "its NovaSeal seal verifies, so the authority can check it offline with "
                "`nova verify`"
            )
        elif seal_state == "unbound":
            check = (
                "its NovaSeal seal verifies but does not bind the evidence streams, so "
                "`nova verify` cannot show the authority that the logs are unchanged"
            )
        else:
            check = (
                "it carries no seal that verifies, so the authority cannot check its "
                "integrity with `nova verify`"
            )
        entries.append(_entry(
            "authority_log_access",
            "the capsule is a self-contained log the provider can hand to a competent "
            f"authority; {check}. Giving access is the provider's act and is not "
            "recorded here",
            complete=False, partial=True,
            source=EvidenceSource.capsule_verified, ref=capsule_ref,
        ))
    else:
        entries.append(_entry(
            "authority_log_access",
            "no automatically-recorded event streams found that could be given to an "
            "authority",
            complete=False, source=EvidenceSource.unverifiable,
        ))

    # Art. 12(3)(b)-(d) — remote biometric identification systems only.
    if remote_biometric_identification:
        entries.append(_entry(
            "rbi_reference_database",
            "NovaFabric capture does not record which reference database input data "
            "was checked against; the system must log it",
            complete=False, source=EvidenceSource.unverifiable,
        ))
        if has_io and capsule_ref is not None:
            entries.append(_entry(
                "rbi_matched_input_data",
                "capsule records model inputs, but does not mark which inputs led to "
                "a match",
                complete=False, partial=True,
                source=EvidenceSource.capsule_verified, ref=capsule_ref,
            ))
        else:
            entries.append(_entry(
                "rbi_matched_input_data",
                "no input records found, and matches are not marked",
                complete=False, source=EvidenceSource.unverifiable,
            ))
        entries.append(_entry(
            "rbi_verifying_persons",
            "NovaFabric capture does not record who verified a result (Art. 14(5)); "
            "the system must log it",
            complete=False, source=EvidenceSource.unverifiable,
        ))

    requirements = [
        Art12Requirement(
            field_name=key, label=label, basis=ART12_NON_ACT_BASIS.get(key, "ai_act"),
        )
        for key, label in art12_requirements(
            remote_biometric_identification=remote_biometric_identification
        )
    ]

    return Art12ConformanceReport(
        deployment_id=deployment_id,
        generated_at=generated_at,
        completeness_summary=entries,
        requirements=requirements,
        remote_biometric_identification=remote_biometric_identification,
    )


__all__ = [
    "ART12_NON_ACT_BASIS",
    "ART12_RBI_REQUIREMENTS",
    "ART12_REQUIREMENTS",
    "ART12_STANDARD",
    "Art12ConformanceReport",
    "Art12Requirement",
    "RequirementBasis",
    "art12_requirements",
    "build_art12_report",
]
