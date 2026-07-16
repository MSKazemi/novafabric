"""Assurance-case defeaters (ADR-0166 D4, first slice).

A **defeater** is a recorded challenge to an argument node — a rebuttal (the claim is
false) or an undercut (the inference doesn't hold). While a defeater is ``open`` it
undermines its target node; the argument is honestly *defeated at that node* until the
defeater is cleared. Clearing requires either ``withdrawn`` (retracted) or ``rebutted``,
and a rebuttal MUST point at the evidence that answers it (``resolved_by``) — you cannot
declare a challenge answered without saying what answered it.

Open defeaters surface as ``defeater_open`` drift records (reusing the D2 drift model),
so a defeated argument is recorded, not silently re-decided.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, model_validator

from novafabric.assure.currency import DriftReason, DriftRecord


class DefeaterState(str, Enum):
    open = "open"  # unanswered — undermines its target
    rebutted = "rebutted"  # answered by counter-evidence (requires resolved_by)
    withdrawn = "withdrawn"  # retracted


class Defeater(BaseModel):
    id: str
    target_node_id: str
    statement: str  # the challenge — a reference/summary, never findings or PII
    state: DefeaterState = DefeaterState.open
    resolved_by: str | None = None  # digest/ref of the rebutting evidence

    @model_validator(mode="after")
    def _rebutted_needs_evidence(self) -> Defeater:
        if self.state is DefeaterState.rebutted and not self.resolved_by:
            raise ValueError("a rebutted defeater must set resolved_by (the rebutting evidence)")
        return self


def is_open(defeater: Defeater) -> bool:
    return defeater.state is DefeaterState.open


def open_defeaters(defeaters: list[Defeater]) -> list[Defeater]:
    return [d for d in defeaters if is_open(d)]


def defeated_nodes(defeaters: list[Defeater]) -> set[str]:
    """Return the set of node ids that currently have at least one open defeater."""
    return {d.target_node_id for d in open_defeaters(defeaters)}


def defeater_drift_records(defeaters: list[Defeater]) -> list[DriftRecord]:
    """Emit a ``defeater_open`` drift record for each open defeater."""
    return [
        DriftRecord(
            node_id=d.target_node_id,
            reason=DriftReason.defeater_open,
            triggered_by=d.id,
        )
        for d in open_defeaters(defeaters)
    ]


def rebut(defeater: Defeater, *, resolved_by: str) -> Defeater:
    """Return a rebutted copy of ``defeater`` bound to the rebutting evidence.

    ``resolved_by`` must be non-empty — a rebuttal without the evidence that answers it
    is not a rebuttal.
    """
    if not resolved_by:
        raise ValueError("resolved_by (the rebutting evidence) is required to rebut a defeater")
    return defeater.model_copy(
        update={"state": DefeaterState.rebutted, "resolved_by": resolved_by}
    )
