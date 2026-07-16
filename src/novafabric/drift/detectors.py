"""Offline two-sample drift detectors over sealed capsule windows (ADR-0147 D2 / NF-151+NF-152).

Pure, **stdlib-only** two-sample statistics computed over already-sealed capsule samples versus a
baseline distribution — **no model re-invocation, zero token cost** (I-2). Three distribution-free
metrics are provided:

* :func:`psi` — Population Stability Index over two numeric samples,
* :func:`ks_statistic` — the two-sample Kolmogorov–Smirnov max-CDF distance (in ``[0, 1]``),
* :func:`jensen_shannon_distance` — the Jensen–Shannon distance over two categorical distributions
  (e.g. a tool-call mix), in ``[0, 1]``.

The record builders wrap a computed value against a ``threshold`` into an output-drift (NF-151) or
behavioral-drift (NF-152) record. NovaFabric **detects and evidences** drift here — ``drifted`` is a
threshold fact, not a promote/pass verdict, and nothing is remediated. Embedding-based output
metrics are optional and pattern-only; these defaults need only the standard library.

This first slice is the detector math + records over supplied samples; the collector that reads the
baseline/window samples from sealed capsules (ADR-0129 offline path, ADR-0132/0133 cost accounting)
is a documented follow-on.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from pydantic import BaseModel

_EPS = 1e-9

#: Two-sample statistics supported for numeric samples.
NUMERIC_STATISTICS: tuple[str, ...] = ("psi", "ks")


def psi(baseline: Sequence[float], window: Sequence[float], *, bins: int = 10) -> float:
    """Population Stability Index between two numeric samples over ``bins`` equal-width buckets.

    ``0`` means identical distributions; larger values mean more shift (the common bands are
    ``<0.1`` no shift, ``0.1–0.25`` moderate, ``>0.25`` significant). Bins span the combined range;
    empty bins are epsilon-smoothed so the log stays finite.
    """
    if not baseline or not window:
        raise ValueError("psi requires non-empty baseline and window samples")
    lo = min(min(baseline), min(window))
    hi = max(max(baseline), max(window))
    if hi - lo <= _EPS:
        return 0.0  # no spread across either sample — no measurable shift
    width = (hi - lo) / bins

    def fractions(sample: Sequence[float]) -> list[float]:
        counts = [0] * bins
        for x in sample:
            idx = min(int((x - lo) / width), bins - 1)
            counts[idx] += 1
        n = len(sample)
        return [c / n for c in counts]

    fb = fractions(baseline)
    fw = fractions(window)
    total = 0.0
    for pb, pw in zip(fb, fw):
        pb = max(pb, _EPS)
        pw = max(pw, _EPS)
        total += (pw - pb) * math.log(pw / pb)
    return total


def ks_statistic(baseline: Sequence[float], window: Sequence[float]) -> float:
    """Two-sample Kolmogorov–Smirnov statistic: the max gap between the two empirical CDFs.

    ``0`` for identical samples, ``1`` for fully disjoint ones.
    """
    if not baseline or not window:
        raise ValueError("ks_statistic requires non-empty baseline and window samples")
    b = sorted(baseline)
    w = sorted(window)
    nb, nw = len(b), len(w)
    points = sorted(set(b) | set(w))

    def cdf(sorted_sample: list[float], n: int, x: float) -> float:
        # fraction of the sample <= x (binary search on the sorted sample)
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_sample[mid] <= x:
                lo = mid + 1
            else:
                hi = mid
        return lo / n

    return max(abs(cdf(b, nb, x) - cdf(w, nw, x)) for x in points)


def _normalize(dist: Mapping[str, float]) -> dict[str, float]:
    total = sum(dist.values())
    if total <= _EPS:
        raise ValueError("distribution must have positive total mass")
    return {k: v / total for k, v in dist.items()}


def jensen_shannon_distance(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """Jensen–Shannon distance (base-2, so in ``[0, 1]``) between two categorical distributions.

    ``0`` for identical distributions, ``1`` for fully disjoint support. Inputs need not be
    pre-normalized; they are normalized to sum to 1 defensively.
    """
    pn = _normalize(p)
    qn = _normalize(q)
    keys = set(pn) | set(qn)

    def kl(a: dict[str, float], b: dict[str, float]) -> float:
        total = 0.0
        for k in keys:
            ak = a.get(k, 0.0)
            bk = b.get(k, 0.0)
            if ak > 0.0 and bk > 0.0:
                total += ak * math.log2(ak / bk)
        return total

    m = {k: (pn.get(k, 0.0) + qn.get(k, 0.0)) / 2 for k in keys}
    jsd = 0.5 * kl(pn, m) + 0.5 * kl(qn, m)
    jsd = min(max(jsd, 0.0), 1.0)  # clamp tiny float error into [0, 1]
    return math.sqrt(jsd)


def _numeric_distance(statistic: str, baseline: Sequence[float], window: Sequence[float]) -> float:
    if statistic == "psi":
        return psi(baseline, window)
    if statistic == "ks":
        return ks_statistic(baseline, window)
    raise ValueError(
        f"unknown numeric statistic {statistic!r}; expected one of: {', '.join(NUMERIC_STATISTICS)}"
    )


class OutputDriftRecord(BaseModel):
    kind: str = "output"
    metric: str  # response-length-dist | output-embedding-dist | score-dist | judge-pass-rate
    statistic: str  # psi | ks
    value: float
    threshold: float
    drifted: bool  # value >= threshold — a fact, never a promote/pass verdict
    window: dict[str, object]  # {from, to, run_ids}
    baseline_id: str | None = None


class BehavioralDriftRecord(BaseModel):
    kind: str = "behavioral"
    dimension: str  # cost-per-run | tokens-per-run | tool-call-mix | tool-error-rate | trajectory
    distance: str  # psi | ks | jensen-shannon
    value: float
    threshold: float
    drifted: bool
    baseline_distribution: dict[str, float] | list[float]
    window_distribution: dict[str, float] | list[float]


def build_output_drift(
    *,
    metric: str,
    statistic: str,
    baseline: Sequence[float],
    window: Sequence[float],
    threshold: float,
    window_meta: Mapping[str, object],
    baseline_id: str | None = None,
) -> OutputDriftRecord:
    """Compute an output-drift record (NF-151) over numeric samples against a ``threshold``."""
    value = _numeric_distance(statistic, baseline, window)
    return OutputDriftRecord(
        metric=metric,
        statistic=statistic,
        value=value,
        threshold=threshold,
        drifted=value >= threshold,
        window=dict(window_meta),
        baseline_id=baseline_id,
    )


def build_behavioral_drift(
    *,
    dimension: str,
    distance: str,
    baseline: Sequence[float] | Mapping[str, float],
    window: Sequence[float] | Mapping[str, float],
    threshold: float,
) -> BehavioralDriftRecord:
    """Compute a behavioral-drift record (NF-152).

    A categorical dimension (a tool-call mix, supplied as mappings) uses Jensen–Shannon distance;
    a numeric dimension (cost/tokens/trajectory, supplied as sequences) uses ``distance`` (``psi`` /
    ``ks``). ``drifted`` is ``value >= threshold``.
    """
    base_out: dict[str, float] | list[float]
    win_out: dict[str, float] | list[float]
    if isinstance(baseline, Mapping) and isinstance(window, Mapping):
        value = jensen_shannon_distance(baseline, window)
        base_out = dict(baseline)
        win_out = dict(window)
    elif isinstance(baseline, Sequence) and isinstance(window, Sequence):
        value = _numeric_distance(distance, list(baseline), list(window))
        base_out = list(baseline)
        win_out = list(window)
    else:
        raise ValueError("baseline and window must both be mappings or both be sequences")
    return BehavioralDriftRecord(
        dimension=dimension,
        distance=distance,
        value=value,
        threshold=threshold,
        drifted=value >= threshold,
        baseline_distribution=base_out,
        window_distribution=win_out,
    )
