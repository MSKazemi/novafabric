"""Failure attribution / root-cause analysis over the lineage graph (ADR-0084).

Public surface:

* :class:`AgentErrorTaxonomy` — src-411 failure categories.
* :func:`attribute_failure` — rank a failed run's steps and label the culprit.
* :class:`RunAttribution`, :class:`StepAttribution` — typed results.
* :func:`verify_hypothesis`, :class:`HypothesisVerification`, :class:`Verdict` —
  intervention-verified attribution, first slice (ADR-0101, experimental).
"""
from __future__ import annotations

from novafabric.diagnose.attribution import (
    AgentErrorTaxonomy,
    CapsuleNotFoundError,
    RunAttribution,
    StepAttribution,
    attribute_failure,
)
from novafabric.diagnose.verify import (
    HypothesisVerification,
    UnmappableHypothesisError,
    Verdict,
    verify_hypothesis,
)

__all__ = [
    "AgentErrorTaxonomy",
    "CapsuleNotFoundError",
    "HypothesisVerification",
    "RunAttribution",
    "StepAttribution",
    "UnmappableHypothesisError",
    "Verdict",
    "attribute_failure",
    "verify_hypothesis",
]
