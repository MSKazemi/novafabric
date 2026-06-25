"""Failure attribution / root-cause analysis over the lineage graph (ADR-0084).

Public surface:

* :class:`AgentErrorTaxonomy` — src-411 failure categories.
* :func:`attribute_failure` — rank a failed run's steps and label the culprit.
* :class:`RunAttribution`, :class:`StepAttribution` — typed results.
"""
from __future__ import annotations

from novafabric.diagnose.attribution import (
    AgentErrorTaxonomy,
    CapsuleNotFoundError,
    RunAttribution,
    StepAttribution,
    attribute_failure,
)

__all__ = [
    "AgentErrorTaxonomy",
    "CapsuleNotFoundError",
    "RunAttribution",
    "StepAttribution",
    "attribute_failure",
]
