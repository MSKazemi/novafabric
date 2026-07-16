"""ADR-0154 D2 / NF-229 — eval-cost / compute disclosure.

The ``eval_cost`` disclosure carries ``wall_seconds``, ``token_in``, ``token_out``, ``usd_cost`` and
MAY carry ``energy_wh`` + ``hardware_ref``. All values are **self-reported** — NovaFabric discloses
what the harness reported; it does not independently measure or certify them.
"""
from __future__ import annotations

import pytest

from novafabric.eval.integrity.cost import EvalCost, build_eval_cost


def test_valid_full_disclosure():
    rec = build_eval_cost(
        wall_seconds=12.5,
        token_in=1000,
        token_out=250,
        usd_cost=0.0123,
        energy_wh=4.2,
        hardware_ref="hw://a100#node3",
    )
    assert isinstance(rec, EvalCost)
    assert rec.wall_seconds == 12.5
    assert rec.token_in == 1000
    assert rec.usd_cost == 0.0123
    assert rec.energy_wh == 4.2
    assert rec.hardware_ref == "hw://a100#node3"
    assert rec.self_reported is True


def test_optional_energy_and_hardware_default_none():
    rec = build_eval_cost(wall_seconds=1.0, token_in=10, token_out=5, usd_cost=0.001)
    assert rec.energy_wh is None
    assert rec.hardware_ref is None


def test_self_reported_is_always_true():
    rec = build_eval_cost(
        wall_seconds=1.0, token_in=1, token_out=1, usd_cost=0.0, self_reported=False
    )
    assert rec.self_reported is True  # a disclosure, never a NovaFabric measurement


@pytest.mark.parametrize(
    "field,value",
    [
        ("wall_seconds", -1.0),
        ("token_in", -5),
        ("token_out", -1),
        ("usd_cost", -0.01),
        ("energy_wh", -2.0),
    ],
)
def test_negative_values_rejected(field, value):
    kwargs = {"wall_seconds": 1.0, "token_in": 1, "token_out": 1, "usd_cost": 0.0}
    kwargs[field] = value
    with pytest.raises(ValueError):
        build_eval_cost(**kwargs)


def test_no_measured_or_verdict_field():
    # A self-reported disclosure — never a measured/certified/verified figure.
    for forbidden in ("measured", "verified", "certified", "verdict", "passed", "audited"):
        assert forbidden not in EvalCost.model_fields
