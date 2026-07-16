"""ADR-0166 D4 — assurance-case defeaters (first slice).

A defeater is a recorded challenge to an argument node (a rebuttal/undercut). While a
defeater is **open** it undermines its target node; it is cleared only by transitioning to
``rebutted`` (which requires pointing at the rebutting evidence) or ``withdrawn``. Open
defeaters surface as ``defeater_open`` drift records.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from novafabric.assure.currency import DriftReason
from novafabric.assure.defeater import (
    Defeater,
    DefeaterState,
    defeated_nodes,
    defeater_drift_records,
    rebut,
)


def test_open_defeater_defeats_its_target_node():
    d = Defeater(id="D1", target_node_id="G1", statement="hazard H7 not covered")
    assert d.state is DefeaterState.open
    assert defeated_nodes([d]) == {"G1"}


def test_rebutted_and_withdrawn_defeaters_do_not_defeat():
    reb = Defeater(id="D1", target_node_id="G1", statement="x",
                   state=DefeaterState.rebutted, resolved_by="d" * 64)
    wd = Defeater(id="D2", target_node_id="G2", statement="y", state=DefeaterState.withdrawn)
    assert defeated_nodes([reb, wd]) == set()


def test_defeated_nodes_aggregates_only_open_targets():
    ds = [
        Defeater(id="D1", target_node_id="G1", statement="a"),
        Defeater(id="D2", target_node_id="S1", statement="b"),
        Defeater(id="D3", target_node_id="G1", statement="c",
                 state=DefeaterState.rebutted, resolved_by="d" * 64),
    ]
    assert defeated_nodes(ds) == {"G1", "S1"}


def test_open_defeaters_emit_defeater_open_drift_records():
    ds = [
        Defeater(id="D1", target_node_id="G1", statement="a"),
        Defeater(id="D2", target_node_id="G2", statement="b", state=DefeaterState.withdrawn),
    ]
    records = defeater_drift_records(ds)
    assert len(records) == 1
    assert records[0].node_id == "G1"
    assert records[0].reason is DriftReason.defeater_open
    assert records[0].triggered_by == "D1"


def test_rebutted_state_requires_resolving_evidence():
    with pytest.raises(ValidationError):
        Defeater(id="D1", target_node_id="G1", statement="x", state=DefeaterState.rebutted)


def test_rebut_transitions_open_to_rebutted_with_evidence():
    d = Defeater(id="D1", target_node_id="G1", statement="x")
    r = rebut(d, resolved_by="e" * 64)
    assert r.state is DefeaterState.rebutted
    assert r.resolved_by == "e" * 64
    assert defeated_nodes([r]) == set()


def test_rebut_requires_non_empty_evidence():
    d = Defeater(id="D1", target_node_id="G1", statement="x")
    with pytest.raises(ValueError):
        rebut(d, resolved_by="")
