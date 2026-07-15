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

"""Tests for NF-007 statistical regression diff (ADR-0099, extends ADR-0080)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import jsonschema

from novafabric.eval.regression_diff import (
    DiffExit,
    SignificanceDiff,
    significance_diff,
)
from novafabric.eval.significance import SprtVerdict

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "significance-diff-v1.schema.json").read_text()
)


def _outcomes(successes: int, n: int) -> list[int]:
    """Deterministic, realistically-*interleaved* pass/fail sequence.

    Spreads the ``n - successes`` failures evenly among the passes (Bresenham), rather
    than clustering them — clustering would let the SPRT early-stop on a run of failures,
    which is not representative of a real run sequence.
    """
    fails = n - successes
    acc = 0
    seq: list[int] = []
    for _ in range(n):
        acc += fails
        if acc >= n:
            acc -= n
            seq.append(0)
        else:
            seq.append(1)
    return seq


# ── SPRT verdict + exit codes (req 1/3, acceptance §6) ───────────────────────


def test_significant_regression_blocks() -> None:
    diff = significance_diff(_outcomes(47, 50), _outcomes(38, 50))
    assert diff.sprt.verdict is SprtVerdict.ACCEPT_H1
    assert diff.is_regression()
    assert diff.exit_code() == int(DiffExit.REGRESSION) == 3


def test_small_dip_does_not_block() -> None:
    diff = significance_diff(_outcomes(47, 50), _outcomes(46, 50))
    assert diff.sprt.verdict is not SprtVerdict.ACCEPT_H1
    assert not diff.is_regression()
    assert diff.exit_code() == 0


def test_single_failure_is_not_a_regression() -> None:
    # Noise guard: one fail among passes → CONTINUE/ACCEPT_H0, never blocks.
    diff = significance_diff(_outcomes(50, 50), _outcomes(19, 20))
    assert diff.sprt.verdict is not SprtVerdict.ACCEPT_H1
    assert diff.exit_code() == 0


def test_wilson_bands_populated() -> None:
    diff = significance_diff(_outcomes(47, 50), _outcomes(38, 50))
    assert diff.baseline.wilson[0] < diff.baseline.wilson[1]
    assert diff.candidate.n == 50 and diff.candidate.successes == 38


def test_param_overrides_flow_through() -> None:
    diff = significance_diff(
        _outcomes(47, 50), _outcomes(38, 50), p0=0.95, p1=0.6, alpha=0.01, beta=0.01
    )
    assert diff.sprt.p0 == 0.95 and diff.sprt.p1 == 0.6
    assert diff.sprt.alpha == 0.01 and diff.sprt.beta == 0.01


def test_run_ids_and_metric_recorded() -> None:
    diff = significance_diff(
        _outcomes(9, 10),
        _outcomes(5, 10),
        metric="task_pass",
        baseline_run_ids=["01HXAY7M5JZ8R7K4P9DPBYK2WX"],
        candidate_run_ids=["01HYAY7M5JZ8R7K4P9DPBYK2WX"],
    )
    assert diff.metric == "task_pass"
    assert diff.baseline.run_ids == ["01HXAY7M5JZ8R7K4P9DPBYK2WX"]


# ── numeric drift, separate from the pass-rate verdict (req 5) ───────────────


def test_numeric_drift_detected() -> None:
    diff = significance_diff(
        _outcomes(45, 50),
        _outcomes(45, 50),
        numeric_baseline=[0.90] * 20,
        numeric_candidate=[0.60] * 20,
    )
    assert diff.numeric is not None
    assert diff.drift is not None
    assert diff.drift.detected is True
    assert diff.drift.method == "welch"
    assert diff.drift.mean_shift < 0


def test_no_numeric_drift_when_stable() -> None:
    diff = significance_diff(
        _outcomes(45, 50),
        _outcomes(45, 50),
        numeric_baseline=[0.80, 0.81, 0.79, 0.80, 0.82],
        numeric_candidate=[0.80, 0.81, 0.79, 0.80, 0.82],
    )
    assert diff.drift is not None
    assert diff.drift.detected is False


def test_numeric_ignored_when_too_few_points() -> None:
    diff = significance_diff(
        _outcomes(45, 50), _outcomes(45, 50), numeric_baseline=[0.8], numeric_candidate=[0.6]
    )
    assert diff.numeric is None
    assert diff.drift is None


# ── fingerprint extension hook (req 8) ───────────────────────────────────────


def test_fingerprint_hook_invoked() -> None:
    def fp(base: Sequence[float], cand: Sequence[float]) -> dict[str, Any]:
        return {"t2": 1.23, "n": len(cand)}

    diff = significance_diff(
        _outcomes(45, 50), _outcomes(40, 50),
        numeric_baseline=[0.8] * 5, numeric_candidate=[0.7] * 5, fingerprint=fp,
    )
    assert diff.fingerprint == {"t2": 1.23, "n": 5}


def test_no_fingerprint_by_default() -> None:
    diff = significance_diff(_outcomes(45, 50), _outcomes(40, 50))
    assert diff.fingerprint is None


# ── schema conformance ───────────────────────────────────────────────────────


def test_diff_validates_against_schema() -> None:
    diff = significance_diff(
        _outcomes(47, 50), _outcomes(38, 50),
        numeric_baseline=[0.9] * 10, numeric_candidate=[0.6] * 10,
    )
    instance = json.loads(diff.model_dump_json())
    jsonschema.validate(instance, _SCHEMA)


def test_roundtrip() -> None:
    diff = significance_diff(_outcomes(47, 50), _outcomes(38, 50))
    again = SignificanceDiff.model_validate_json(diff.model_dump_json())
    assert again == diff
