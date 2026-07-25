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
"""Span-level claim grounding audit (ADR-0101 §NF-021, structural slice).

Over a capsule's recorded span tree, a **claim** is a model/generation span (it asserts
content) and **evidence** is a tool/retrieval span (it grounds content). This module marks
each claim ``grounded`` or ``ungrounded`` by whether any evidence span precedes it on the
answer path — a *structural* signal, deterministic and LLM-free. An ungrounded claim (a
model output produced with no prior tool/retrieval grounding) is a hallucination-*risk*
finding, not a semantic truth judgment.

It reuses the diagnose step loader (``attribution._load_steps``) and composes with the
causal-graph attribution (:mod:`novafabric.diagnose.causal_graph`); a claim marked
ungrounded is the kind of scored, sealed finding the ADR-0099 eval layer consumes.

**Deferred (unchanged design intent):** *conflicting*-claim detection — flagging two claims
that contradict each other — is a semantic comparison and is future design; this slice
ships the grounding (supported / unsupported) half only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from novafabric.diagnose.attribution import CapsuleNotFoundError, _load_steps

#: Span kinds treated as claims (assert content) and as evidence (ground content).
_CLAIM_KIND = "model"
_EVIDENCE_KIND = "tool"


class ClaimGrounding(str, Enum):
    """Whether a claim span is grounded by prior evidence on the answer path."""

    grounded = "grounded"
    ungrounded = "ungrounded"


@dataclass
class ClaimFinding:
    """One claim span and its grounding status."""

    step_id: str
    name: str
    grounding: ClaimGrounding
    supporting_evidence_ids: list[str]
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "grounding": self.grounding.value,
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "rationale": self.rationale,
        }


@dataclass
class ClaimAudit:
    """Grounding audit over a capsule's claim spans."""

    run_id: str
    claims: list[ClaimFinding] = field(default_factory=list)
    n_claims: int = 0
    n_ungrounded: int = 0
    note: str = (
        "Structural grounding audit (NF-021): a claim with no prior evidence span on the "
        "answer path is a hallucination-risk finding, not a semantic truth judgment. "
        "Conflicting-claim detection is deferred."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "n_claims": self.n_claims,
            "n_ungrounded": self.n_ungrounded,
            "claims": [c.as_dict() for c in self.claims],
            "note": self.note,
        }


def audit_claims(capsule_dir: Path | str) -> ClaimAudit:
    """Audit the grounding of every claim span in a capsule.

    Pure and read-only. A claim (a ``model`` span) is ``grounded`` when at least one
    evidence (``tool``) span precedes it in rollout order; the preceding evidence ids are
    carried on the finding. A claim with no prior evidence is ``ungrounded``. Raises
    :class:`CapsuleNotFoundError` if there is no ``capsule.yaml``.
    """
    capsule_dir = Path(capsule_dir)
    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        raise CapsuleNotFoundError(f"No capsule.yaml under {capsule_dir}")
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    run_id = str(manifest.get("run_id", capsule_dir.name))

    steps = _load_steps(capsule_dir, manifest)  # already ordered earliest-first

    findings: list[ClaimFinding] = []
    n_ungrounded = 0
    for step in steps:
        if step.kind != _CLAIM_KIND:
            continue
        # Evidence that precedes this claim in the answer path (earlier rollout ordinal).
        evidence_ids = [
            s.step_id
            for s in steps
            if s.kind == _EVIDENCE_KIND and s.ordinal < step.ordinal
        ]
        if evidence_ids:
            grounding = ClaimGrounding.grounded
            rationale = (
                f"claim '{step.name}' grounded by {len(evidence_ids)} prior evidence "
                f"span(s) on the answer path"
            )
        else:
            grounding = ClaimGrounding.ungrounded
            n_ungrounded += 1
            rationale = (
                f"claim '{step.name}' has no supporting evidence span before it on the "
                f"answer path — hallucination-risk finding (structural, not a semantic "
                f"truth judgment)"
            )
        findings.append(
            ClaimFinding(
                step_id=step.step_id,
                name=step.name,
                grounding=grounding,
                supporting_evidence_ids=evidence_ids,
                rationale=rationale,
            )
        )

    return ClaimAudit(
        run_id=run_id,
        claims=findings,
        n_claims=len(findings),
        n_ungrounded=n_ungrounded,
    )
