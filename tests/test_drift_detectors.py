"""ADR-0147 D2 / NF-151+NF-152 — offline two-sample drift detectors over sealed windows.

Pure, stdlib-only two-sample statistics (PSI / KS / Jensen-Shannon) computed over already-sealed
capsule samples versus a baseline — **no model re-invocation, zero token cost**. It detects and
evidences drift (a `drifted` threshold fact); it never remediates and issues no promote/pass verdict.
"""
from __future__ import annotations

import math

import pytest

from novafabric.drift.detectors import (
    BehavioralDriftRecord,
    OutputDriftRecord,
    build_behavioral_drift,
    build_output_drift,
    jensen_shannon_distance,
    ks_statistic,
    psi,
)

# --- PSI --------------------------------------------------------------------------------------

def test_psi_identical_is_zero():
    x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    assert psi(x, list(x)) == pytest.approx(0.0, abs=1e-9)


def test_psi_shifted_is_large():
    baseline = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
    window = [9.0, 9.1, 9.2, 9.3, 9.4, 9.5]  # fully shifted away
    assert psi(baseline, window) > 0.25  # well past the usual "significant shift" band


def test_psi_empty_raises():
    with pytest.raises(ValueError):
        psi([], [1.0, 2.0])


# --- KS ---------------------------------------------------------------------------------------

def test_ks_identical_is_zero():
    x = [1.0, 2.0, 3.0, 4.0]
    assert ks_statistic(x, list(x)) == pytest.approx(0.0, abs=1e-9)


def test_ks_disjoint_is_one():
    assert ks_statistic([0.0, 0.0, 0.0], [1.0, 1.0, 1.0]) == pytest.approx(1.0)


def test_ks_between_zero_and_one():
    d = ks_statistic([0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0])
    assert 0.0 < d < 1.0


# --- Jensen-Shannon distance (categorical, e.g. tool-call-mix) --------------------------------

def test_js_identical_is_zero():
    p = {"search": 0.4, "db.query": 0.35, "email.send": 0.25}
    assert jensen_shannon_distance(p, dict(p)) == pytest.approx(0.0, abs=1e-9)


def test_js_disjoint_is_one():
    assert jensen_shannon_distance({"a": 1.0}, {"b": 1.0}) == pytest.approx(1.0, abs=1e-9)


def test_js_is_symmetric():
    p = {"a": 0.7, "b": 0.3}
    q = {"a": 0.2, "b": 0.8}
    assert jensen_shannon_distance(p, q) == pytest.approx(jensen_shannon_distance(q, p))


def test_js_in_unit_interval():
    d = jensen_shannon_distance({"a": 0.6, "b": 0.4}, {"a": 0.1, "b": 0.9})
    assert 0.0 < d < 1.0
    assert not math.isnan(d)


# --- output-drift record ----------------------------------------------------------------------

def test_build_output_drift_flags_drifted_over_threshold():
    rec = build_output_drift(
        metric="response-length-dist",
        statistic="psi",
        baseline=[10.0, 11.0, 12.0, 13.0, 14.0],
        window=[40.0, 41.0, 42.0, 43.0, 44.0],
        threshold=0.20,
        window_meta={"from": "2026-07-05", "to": "2026-07-12", "run_ids": ["r1", "r2"]},
        baseline_id="bl-2026Q2",
    )
    assert isinstance(rec, OutputDriftRecord)
    assert rec.kind == "output"
    assert rec.statistic == "psi"
    assert rec.value > 0.20
    assert rec.drifted is True
    assert rec.window["run_ids"] == ["r1", "r2"]
    assert rec.baseline_id == "bl-2026Q2"


def test_build_output_drift_not_drifted_when_stable():
    x = [10.0, 11.0, 12.0, 13.0, 14.0]
    rec = build_output_drift(
        metric="score-dist", statistic="psi", baseline=x, window=list(x),
        threshold=0.20, window_meta={"from": "a", "to": "b", "run_ids": []},
    )
    assert rec.drifted is False


def test_build_output_drift_rejects_unknown_statistic():
    with pytest.raises(ValueError):
        build_output_drift(
            metric="score-dist", statistic="chi2", baseline=[1.0], window=[1.0],
            threshold=0.1, window_meta={},
        )


# --- behavioral-drift record ------------------------------------------------------------------

def test_build_behavioral_drift_tool_mix_uses_js():
    rec = build_behavioral_drift(
        dimension="tool-call-mix",
        distance="jensen-shannon",
        baseline={"search": 0.4, "db.query": 0.35, "email.send": 0.25},
        window={"search": 0.6, "db.query": 0.30, "email.send": 0.10},
        threshold=0.10,
    )
    assert isinstance(rec, BehavioralDriftRecord)
    assert rec.kind == "behavioral"
    assert rec.dimension == "tool-call-mix"
    assert rec.value > 0.0
    assert isinstance(rec.drifted, bool)


def test_build_behavioral_drift_numeric_dimension_uses_psi():
    rec = build_behavioral_drift(
        dimension="cost-per-run",
        distance="psi",
        baseline=[0.01, 0.011, 0.012, 0.013],
        window=[0.05, 0.051, 0.052, 0.053],
        threshold=0.20,
    )
    assert rec.value > 0.0
    assert rec.drifted is True


def test_records_have_no_remediation_or_promote_verdict():
    # ADR-0147: NovaFabric detects and evidences drift; it never remediates or gates promotion here.
    for model in (OutputDriftRecord, BehavioralDriftRecord):
        for forbidden in ("remediated", "promote", "passed", "failed", "verdict", "gate"):
            assert forbidden not in model.model_fields
