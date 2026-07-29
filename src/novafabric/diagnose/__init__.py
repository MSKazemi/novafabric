"""Failure attribution / root-cause analysis over the lineage graph (ADR-0084).

Public surface:

* :class:`AgentErrorTaxonomy` — src-411 failure categories.
* :func:`attribute_failure` — rank a failed run's steps and label the culprit.
* :class:`RunAttribution`, :class:`StepAttribution` — typed results.
* :func:`verify_hypothesis`, :class:`HypothesisVerification`, :class:`Verdict` —
  intervention-verified attribution, first slice (ADR-0101, experimental).
* :func:`causal_root_candidates`, :class:`CausalAttribution`,
  :class:`CausalRootCandidate` — No-LLM causal-graph back-trace over recorded spans
  (ADR-0101 §NF-019); candidates are verification-gated ``unverified`` (§NF-022).
* :func:`search_root_cause`, :class:`RootCauseSearch`, :class:`RootCauseAttempt` —
  counterfactual root-cause search (ADR-0101 §NF-018): sweeps NF-019 causal-root
  candidates with bounded, zero-token intervention replays for the earliest
  confirmed outcome-flipping step.
"""
from __future__ import annotations

from novafabric.diagnose.attribution import (
    AgentErrorTaxonomy,
    CapsuleNotFoundError,
    RunAttribution,
    StepAttribution,
    attribute_failure,
)
from novafabric.diagnose.causal_graph import (
    CausalAttribution,
    CausalRootCandidate,
    causal_root_candidates,
)
from novafabric.diagnose.claim_audit import (
    ClaimAudit,
    ClaimFinding,
    ClaimGrounding,
    audit_claims,
)
from novafabric.diagnose.verify import (
    HypothesisVerification,
    RootCauseAttempt,
    RootCauseSearch,
    UnmappableHypothesisError,
    Verdict,
    search_root_cause,
    verify_hypothesis,
)

__all__ = [
    "AgentErrorTaxonomy",
    "CapsuleNotFoundError",
    "CausalAttribution",
    "CausalRootCandidate",
    "ClaimAudit",
    "ClaimFinding",
    "ClaimGrounding",
    "HypothesisVerification",
    "RootCauseAttempt",
    "RootCauseSearch",
    "RunAttribution",
    "StepAttribution",
    "UnmappableHypothesisError",
    "Verdict",
    "attribute_failure",
    "audit_claims",
    "causal_root_candidates",
    "search_root_cause",
    "verify_hypothesis",
]
