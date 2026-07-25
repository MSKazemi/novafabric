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
"""ISO/IEC 42001 + 42005 evidence exporters (ADR-0107 §NF-095).

A **pure projection** over evidence NovaFabric already holds — an assembler, not a
content generator. Two artifacts:

* :func:`build_iso42001_mapping` maps a *declared* ISO/IEC 42001 control catalog to the
  NovaFabric governance evidence *present* for a capsule, marking each control
  ``evidenced`` / ``not_evidenced`` / ``declared`` and carrying a re-performable
  :class:`~novafabric.compliance.export.provenance.EvidenceSourceRef` for evidenced
  controls. It reuses the ADR-0087/ADR-0170 criterion→evidence pattern
  (``control_attestation.py``): a control's evidence is *bound*, never fabricated. A
  control with absent evidence is an honest gap; the mapping certifies presence of
  evidence, never that a control is *adequate*.
* :func:`build_iso42005_impact_assessment` emits a structured ISO/IEC 42005 AI-system
  impact-assessment object whose canonical sections each record whether they were
  capsule-sourced or operator-declared.

Pure-code and offline: no infrastructure, no new dependencies. ADR-0107's other
exporters (NF-091/093/094/097) remain future design; this ships NF-095.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from novafabric.compliance.export.provenance import (
    EvidenceSource,
    EvidenceSourceRef,
)

_ISO_42001_STANDARD = "ISO/IEC 42001:2023"
_ISO_42005_STANDARD = "ISO/IEC 42005:2025"

#: Canonical ISO/IEC 42005 AI-system impact-assessment sections. The exporter always
#: emits this skeleton so a missing section is a visible gap, never an absent one.
_ISO_42005_SECTIONS: tuple[tuple[str, str], ...] = (
    ("intended_purpose", "Intended purpose and context of use"),
    ("affected_stakeholders", "Affected individuals, groups, and society"),
    ("potential_harms", "Reasonably foreseeable harms and benefits"),
    ("likelihood_severity", "Likelihood and severity assessment"),
    ("mitigation_measures", "Risk mitigation and control measures"),
    ("residual_risk", "Residual risk and accountability sign-off"),
)


class Iso42001ControlStatus(str, Enum):
    """The evidentiary status of an ISO/IEC 42001 control for a capsule."""

    evidenced = "evidenced"
    """The control's mapped governance evidence is present (a ref is carried)."""

    not_evidenced = "not_evidenced"
    """The control's evidence is absent — an honest gap, never fabricated."""

    declared = "declared"
    """The operator asserts the control without NovaFabric evidence backing it."""


class Iso42001ControlMapping(BaseModel):
    """One control's evidence binding."""

    control_id: str
    evidence_kind: str | None = None
    status: Iso42001ControlStatus
    provenance: EvidenceSource
    evidence_ref: EvidenceSourceRef | None = None


class Iso42001ControlReport(BaseModel):
    """A capsule's ISO/IEC 42001 control-evidence mapping."""

    standard: str = _ISO_42001_STANDARD
    controls: list[Iso42001ControlMapping] = Field(default_factory=list)
    evidenced_count: int = 0
    generated_at: str


class Iso42005Section(BaseModel):
    """One section of an ISO/IEC 42005 impact assessment."""

    section_id: str
    title: str
    content_source: EvidenceSource
    populated: bool
    capsule_ref: EvidenceSourceRef | None = None


class Iso42005ImpactAssessment(BaseModel):
    """A structured ISO/IEC 42005 AI-system impact-assessment object."""

    standard: str = _ISO_42005_STANDARD
    system_name: str
    sections: list[Iso42005Section] = Field(default_factory=list)
    generated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_iso42001_mapping(
    catalog: Sequence[Mapping[str, Any]],
    present_evidence: Iterable[str],
    *,
    capsule_ref: EvidenceSourceRef | None = None,
    declared_controls: Iterable[str] | None = None,
) -> Iso42001ControlReport:
    """Map a declared ISO/IEC 42001 control catalog to present capsule evidence.

    Each catalog entry is ``{control_id, evidence_kind?}``. Resolution, per control:

    * in ``declared_controls`` → ``declared`` (operator asserts it; ``operator_asserted``);
    * else its ``evidence_kind`` is in ``present_evidence`` **and** a ``capsule_ref`` was
      supplied → ``evidenced`` (``operator_asserted`` — the ref is carried, not
      re-performed here) with the ref attached;
    * else → ``not_evidenced`` (``unverifiable`` — an honest gap).

    The report certifies presence of evidence, never that a control is adequate.
    """
    present = set(present_evidence)
    declared = set(declared_controls or ())
    controls: list[Iso42001ControlMapping] = []
    evidenced = 0
    for item in catalog:
        control_id = str(item["control_id"])
        evidence_kind = item.get("evidence_kind")
        if control_id in declared:
            controls.append(
                Iso42001ControlMapping(
                    control_id=control_id,
                    evidence_kind=evidence_kind,
                    status=Iso42001ControlStatus.declared,
                    provenance=EvidenceSource.operator_asserted,
                )
            )
            continue
        if evidence_kind in present and capsule_ref is not None:
            evidenced += 1
            controls.append(
                Iso42001ControlMapping(
                    control_id=control_id,
                    evidence_kind=evidence_kind,
                    status=Iso42001ControlStatus.evidenced,
                    provenance=EvidenceSource.operator_asserted,
                    evidence_ref=capsule_ref,
                )
            )
            continue
        controls.append(
            Iso42001ControlMapping(
                control_id=control_id,
                evidence_kind=evidence_kind,
                status=Iso42001ControlStatus.not_evidenced,
                provenance=EvidenceSource.unverifiable,
            )
        )
    return Iso42001ControlReport(
        controls=controls,
        evidenced_count=evidenced,
        generated_at=_now(),
    )


def build_iso42005_impact_assessment(
    system_name: str,
    *,
    sourced_sections: Mapping[str, EvidenceSourceRef] | None = None,
    operator_sections: Iterable[str] | None = None,
) -> Iso42005ImpactAssessment:
    """Emit an ISO/IEC 42005 impact-assessment skeleton bound to a system's evidence.

    Every canonical section (:data:`_ISO_42005_SECTIONS`) is always emitted so a gap is
    visible. Per section:

    * named in ``sourced_sections`` → ``capsule_verified``, populated, with its ref;
    * else named in ``operator_sections`` → ``operator_asserted``, populated, no ref;
    * else → ``operator_asserted``, **unpopulated** (an empty section awaiting content).

    Section names not in the canonical set are ignored — the exporter never fabricates a
    non-standard section.
    """
    sourced = dict(sourced_sections or {})
    operator = set(operator_sections or ())
    sections: list[Iso42005Section] = []
    for section_id, title in _ISO_42005_SECTIONS:
        if section_id in sourced:
            sections.append(
                Iso42005Section(
                    section_id=section_id,
                    title=title,
                    content_source=EvidenceSource.capsule_verified,
                    populated=True,
                    capsule_ref=sourced[section_id],
                )
            )
        elif section_id in operator:
            sections.append(
                Iso42005Section(
                    section_id=section_id,
                    title=title,
                    content_source=EvidenceSource.operator_asserted,
                    populated=True,
                )
            )
        else:
            sections.append(
                Iso42005Section(
                    section_id=section_id,
                    title=title,
                    content_source=EvidenceSource.operator_asserted,
                    populated=False,
                )
            )
    return Iso42005ImpactAssessment(
        system_name=system_name,
        sections=sections,
        generated_at=_now(),
    )
