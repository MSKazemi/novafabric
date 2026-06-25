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

"""Statistical-significance primitives for regression / eval gating (ADR-0080, gap-004).

Pure standard-library functions (`math`/`statistics` only). They make NovaFabric's
behavioral-diff and eval-gated promotion statistically sound and bounded-cost:

- :func:`wilson_interval` — Wilson score confidence interval for a binomial
  pass-rate (replaces bare point scores; src-405).
- :func:`sprt_bernoulli` — Wald Sequential Probability Ratio Test over a pass/fail
  sequence, returning a three-valued verdict so a gate can PASS, FAIL, or *defer*
  for more evidence instead of firing on noise (src-415, src-406).

Both run offline on already-stored capsules at zero token cost.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import Enum
from statistics import NormalDist


class SprtVerdict(str, Enum):
    """Three-valued SPRT outcome for a regression gate."""

    ACCEPT_H0 = "accept_h0"  # acceptable pass-rate holds → no regression (PASS)
    ACCEPT_H1 = "accept_h1"  # regression-threshold pass-rate → regression (FAIL)
    CONTINUE = "continue"  # inconclusive → collect more observations


def wilson_interval(
    successes: int, n: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    Returns ``(lo, hi)`` clamped to ``[0.0, 1.0]``. For ``n == 0`` the interval is
    the full ``(0.0, 1.0)`` (no information). Raises :class:`ValueError` for
    out-of-range counts or confidence.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")
    if n < 0:
        raise ValueError("n must be >= 0")
    if not 0 <= successes <= n:
        raise ValueError("successes must satisfy 0 <= successes <= n")
    if n == 0:
        return (0.0, 1.0)

    z = NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


def sprt_bernoulli(
    observations: Sequence[int],
    p0: float,
    p1: float,
    alpha: float = 0.05,
    beta: float = 0.05,
) -> tuple[SprtVerdict, float]:
    """Wald SPRT over a Bernoulli (pass/fail) sequence.

    ``p0`` is the acceptable pass-rate (H0) and ``p1`` the regression-threshold
    pass-rate (H1); they must satisfy ``0 < p1 < p0 < 1``. ``alpha``/``beta`` are
    the false-positive / false-negative budgets in ``(0, 1)``. Each observation is
    ``1`` (pass) or ``0`` (fail).

    Returns ``(verdict, llr)`` where ``llr`` is the cumulative log-likelihood ratio
    at the point of decision (or after the last observation if inconclusive). The
    test early-stops at the first boundary crossing.
    """
    if not 0.0 < p1 < p0 < 1.0:
        raise ValueError("hypotheses must satisfy 0 < p1 < p0 < 1")
    if not 0.0 < alpha < 1.0 or not 0.0 < beta < 1.0:
        raise ValueError("alpha and beta must be in (0, 1)")

    upper = math.log((1.0 - beta) / alpha)  # cross → accept H1 (regression)
    lower = math.log(beta / (1.0 - alpha))  # cross → accept H0 (no regression)
    step_pass = math.log(p1 / p0)
    step_fail = math.log((1.0 - p1) / (1.0 - p0))

    llr = 0.0
    for x in observations:
        if x == 1:
            llr += step_pass
        elif x == 0:
            llr += step_fail
        else:
            raise ValueError("observations must be 0 or 1")
        if llr >= upper:
            return (SprtVerdict.ACCEPT_H1, llr)
        if llr <= lower:
            return (SprtVerdict.ACCEPT_H0, llr)
    return (SprtVerdict.CONTINUE, llr)
