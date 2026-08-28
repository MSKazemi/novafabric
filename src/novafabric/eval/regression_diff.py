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

"""Statistical regression diff (NF-007, ADR-0099; extends ADR-0080).

Compares a *baseline* vs *candidate* run set by **statistical significance, not raw
delta**: a Wilson interval per side and a Wald SPRT over the candidate sequence, yielding
a three-valued verdict (``ACCEPT_H0`` / ``ACCEPT_H1`` / ``CONTINUE``). Agentic pass@1
swings several points as noise even at temperature 0, so a bare delta gate fires on noise;
this fires **only** on a statistically significant regression (``ACCEPT_H1``).

Design invariants (``the private design/spec/features/NF-007-009-statistical-regression-diff.md``):

- **Zero-token, offline (req 2):** pure arithmetic over already-stored pass/fail outcomes
  and numeric scores; never re-invokes a workload, never calls a model.
- **Reuses the shipped ADR-0080 spine (req 1, G5):** ``wilson_interval`` / ``sprt_bernoulli``
  are used unchanged; no new dependency (stdlib ``math``/``statistics`` only).
- **Drift ≠ regression (req 5):** for numeric metrics a mean-shift + ``drift`` boolean is
  reported *separately* from the pass-rate verdict.
- **Fingerprint hook (req 8):** an optional ``fingerprint`` callable is a named W2 extension
  point (NF-008 Hotelling T²) that never changes the record shape.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from novafabric.capture._ulid import new_ulid
from novafabric.eval.significance import SprtVerdict, sprt_bernoulli, wilson_interval

# ADR-0080 default hypotheses / error budgets (req 4).
DEFAULT_P0 = 0.9
DEFAULT_P1 = 0.7
DEFAULT_ALPHA = 0.05
DEFAULT_BETA = 0.05

#: Signature of the optional NF-008 behavioral-fingerprint hook (req 8).
FingerprintFn = Callable[[Sequence[float], Sequence[float]], dict[str, Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiffExit(int, Enum):
    """Process exit codes for the diff gate (spec §4.2)."""

    NO_BLOCK = 0  # ACCEPT_H0 / CONTINUE
    REGRESSION = 3  # ACCEPT_H1 — significant regression


class ProportionSide(BaseModel):
    """One side of a pass/fail comparison with its Wilson band."""

    model_config = ConfigDict(extra="forbid")

    successes: int = Field(ge=0)
    n: int = Field(ge=0)
    run_ids: list[str] = Field(default_factory=list)
    wilson: tuple[float, float]


class SprtBlock(BaseModel):
    """Wald SPRT outcome + the parameters it ran under."""

    model_config = ConfigDict(extra="forbid")

    verdict: SprtVerdict
    llr: float
    p0: float
    p1: float
    alpha: float
    beta: float


class NumericSummary(BaseModel):
    """Baseline/candidate numeric means (companion to ``drift``)."""

    model_config = ConfigDict(extra="forbid")

    baseline_mean: float
    candidate_mean: float
    baseline_n: int
    candidate_n: int


class DriftBlock(BaseModel):
    """Distribution-shift signal — reported separately from the pass-rate verdict (req 5)."""

    model_config = ConfigDict(extra="forbid")

    detected: bool
    method: str
    mean_shift: float
    ci: tuple[float, float]


class SignificanceDiff(BaseModel):
    """A statistically-grounded behavioral regression diff (see schema)."""

    model_config = ConfigDict(extra="forbid")

    diff_id: str = Field(default_factory=new_ulid)
    metric: str = Field(min_length=1)
    baseline: ProportionSide
    candidate: ProportionSide
    sprt: SprtBlock
    numeric: NumericSummary | None = None
    drift: DriftBlock | None = None
    fingerprint: dict[str, Any] | None = None
    created_at: str = Field(default_factory=_now_iso)

    def is_regression(self) -> bool:
        """True iff the SPRT verdict is a significant regression (req 3 gate)."""
        return self.sprt.verdict is SprtVerdict.ACCEPT_H1

    def exit_code(self) -> int:
        """Process exit code: 3 on a significant regression, else 0 (spec §4.2)."""
        return int(DiffExit.REGRESSION if self.is_regression() else DiffExit.NO_BLOCK)


def _numeric_drift(
    baseline_values: Sequence[float],
    candidate_values: Sequence[float],
    confidence: float,
) -> tuple[NumericSummary, DriftBlock]:
    """Welch normal-approximation mean-shift + drift for numeric metrics (req 5).

    Uses a normal (z) approximation for the difference-of-means CI — stdlib only, no
    ``scipy`` (ADR-0080). ``detected`` is True iff the CI excludes 0.
    """
    b_mean = statistics.fmean(baseline_values)
    c_mean = statistics.fmean(candidate_values)
    mean_shift = c_mean - b_mean
    b_var = statistics.variance(baseline_values)
    c_var = statistics.variance(candidate_values)
    se = math.sqrt(b_var / len(baseline_values) + c_var / len(candidate_values))
    z = statistics.NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    lo, hi = mean_shift - z * se, mean_shift + z * se
    detected = not (lo <= 0.0 <= hi)
    summary = NumericSummary(
        baseline_mean=b_mean,
        candidate_mean=c_mean,
        baseline_n=len(baseline_values),
        candidate_n=len(candidate_values),
    )
    drift = DriftBlock(detected=detected, method="welch", mean_shift=mean_shift, ci=(lo, hi))
    return summary, drift


def significance_diff(
    baseline_outcomes: Sequence[int],
    candidate_outcomes: Sequence[int],
    *,
    metric: str = "task_pass",
    p0: float = DEFAULT_P0,
    p1: float = DEFAULT_P1,
    alpha: float = DEFAULT_ALPHA,
    beta: float = DEFAULT_BETA,
    confidence: float = 0.95,
    baseline_run_ids: Sequence[str] | None = None,
    candidate_run_ids: Sequence[str] | None = None,
    numeric_baseline: Sequence[float] | None = None,
    numeric_candidate: Sequence[float] | None = None,
    fingerprint: FingerprintFn | None = None,
) -> SignificanceDiff:
    """Compare baseline vs candidate pass/fail outcomes by statistical significance.

    ``*_outcomes`` are sequences of ``1`` (pass) / ``0`` (fail) read from stored capsules.
    The SPRT runs over ``candidate_outcomes`` unchanged; the verdict is the only thing a
    gate should block on (``ACCEPT_H1``). Optional numeric sequences add a drift signal
    reported separately. ``fingerprint`` is an optional NF-008 extension point.
    """
    base = ProportionSide(
        successes=sum(baseline_outcomes),
        n=len(baseline_outcomes),
        run_ids=list(baseline_run_ids or []),
        wilson=wilson_interval(sum(baseline_outcomes), len(baseline_outcomes), confidence),
    )
    cand = ProportionSide(
        successes=sum(candidate_outcomes),
        n=len(candidate_outcomes),
        run_ids=list(candidate_run_ids or []),
        wilson=wilson_interval(sum(candidate_outcomes), len(candidate_outcomes), confidence),
    )
    verdict, llr = sprt_bernoulli(list(candidate_outcomes), p0=p0, p1=p1, alpha=alpha, beta=beta)
    sprt = SprtBlock(verdict=verdict, llr=llr, p0=p0, p1=p1, alpha=alpha, beta=beta)

    numeric: NumericSummary | None = None
    drift: DriftBlock | None = None
    if numeric_baseline is not None and numeric_candidate is not None:
        if len(numeric_baseline) >= 2 and len(numeric_candidate) >= 2:
            numeric, drift = _numeric_drift(numeric_baseline, numeric_candidate, confidence)

    fp: dict[str, Any] | None = None
    if fingerprint is not None:
        fp = fingerprint(numeric_baseline or [], numeric_candidate or [])

    return SignificanceDiff(
        metric=metric,
        baseline=base,
        candidate=cand,
        sprt=sprt,
        numeric=numeric,
        drift=drift,
        fingerprint=fp,
    )
