"""Eval-cost / compute disclosure (ADR-0154 D2 / NF-229, first slice).

A pure disclosure record of what an eval run cost: ``wall_seconds``, ``token_in``, ``token_out``,
``usd_cost`` (all required), and optionally ``energy_wh`` + ``hardware_ref`` for carbon-aware
reporting. Per NF-229 every value is **self-reported** — NovaFabric discloses what the harness
reported; it does **not** independently measure, verify, or certify these figures. The record is
therefore always flagged ``self_reported`` and carries no measured/verified/verdict field.

This first slice is the disclosure model + builder over supplied values; the capture-side facet that
populates ``facets.eval_cost`` on the run capsule is a documented follow-on (additive, optional — a
capsule without it stays valid).
"""
from __future__ import annotations

from pydantic import BaseModel

#: The evaluation-integrity honesty line the NF-221-230 spec makes normative.
#: A model field, not a CLI-only print, so `--json` carries it too.
HONESTY_LINE: str = (
    "NovaFabric discloses the cost/compute figures the harness self-reported. It does "
    "not measure, verify, or certify them, and it does not run the eval."
)


class EvalCost(BaseModel):
    wall_seconds: float
    token_in: int
    token_out: int
    usd_cost: float
    energy_wh: float | None = None  # optional, for carbon-aware reporting
    hardware_ref: str | None = None  # optional reference to the compute used
    self_reported: bool = True  # NF-229: self-reported values, never a NovaFabric measurement
    honesty_line: str = HONESTY_LINE


def build_eval_cost(
    *,
    wall_seconds: float,
    token_in: int,
    token_out: int,
    usd_cost: float,
    energy_wh: float | None = None,
    hardware_ref: str | None = None,
    self_reported: bool = True,  # accepted for symmetry but always forced True below
) -> EvalCost:
    """Assemble a self-reported eval-cost disclosure from supplied values.

    All required figures (and ``energy_wh`` when given) must be non-negative. ``self_reported`` is
    always forced ``True`` — this is a disclosure of the harness's figures, never a measurement.
    """
    for name, value in (
        ("wall_seconds", wall_seconds),
        ("token_in", token_in),
        ("token_out", token_out),
        ("usd_cost", usd_cost),
    ):
        if value < 0:
            raise ValueError(f"{name} must be non-negative, got {value}")
    if energy_wh is not None and energy_wh < 0:
        raise ValueError(f"energy_wh must be non-negative, got {energy_wh}")

    return EvalCost(
        wall_seconds=wall_seconds,
        token_in=token_in,
        token_out=token_out,
        usd_cost=usd_cost,
        energy_wh=energy_wh,
        hardware_ref=hardware_ref,
        self_reported=True,  # forced — a disclosure, never a measurement
    )
