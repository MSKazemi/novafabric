"""Tests for the statistical-significance eval-gate primitives (gap-004, ADR-0080).

Wilson confidence interval + Wald SPRT three-valued regression verdict. Pure
functions, deterministic inputs — no model invocation.
"""

import math

import pytest

from novafabric.eval.significance import (
    SprtVerdict,
    sprt_bernoulli,
    wilson_interval,
)

# ── Wilson confidence interval ──────────────────────────────────────────


def test_wilson_interval_brackets_point_estimate() -> None:
    lo, hi = wilson_interval(successes=8, n=10, confidence=0.95)
    assert 0.0 <= lo < 0.8 < hi <= 1.0  # contains the 0.8 point estimate
    assert lo == pytest.approx(0.49, abs=0.03)
    assert hi == pytest.approx(0.943, abs=0.03)


def test_wilson_interval_within_unit_range_at_extremes() -> None:
    lo, hi = wilson_interval(successes=10, n=10, confidence=0.95)
    assert 0.0 <= lo <= hi <= 1.0
    assert hi <= 1.0 and lo > 0.6  # all-success still has uncertainty below 1


def test_wilson_interval_n_zero_is_full_range() -> None:
    assert wilson_interval(successes=0, n=0) == (0.0, 1.0)


def test_wilson_interval_rejects_bad_counts() -> None:
    with pytest.raises(ValueError):
        wilson_interval(successes=11, n=10)
    with pytest.raises(ValueError):
        wilson_interval(successes=-1, n=10)


# ── SPRT three-valued regression verdict ────────────────────────────────


def test_sprt_all_pass_accepts_no_regression() -> None:
    # Long run of passes ⇒ accept H0 (acceptable pass-rate), i.e. no regression.
    obs = [1] * 40
    verdict, llr = sprt_bernoulli(obs, p0=0.9, p1=0.6, alpha=0.05, beta=0.05)
    assert verdict is SprtVerdict.ACCEPT_H0
    assert math.isfinite(llr)


def test_sprt_all_fail_detects_regression() -> None:
    obs = [0] * 40
    verdict, _ = sprt_bernoulli(obs, p0=0.9, p1=0.6, alpha=0.05, beta=0.05)
    assert verdict is SprtVerdict.ACCEPT_H1  # regression detected


def test_sprt_insufficient_evidence_continues() -> None:
    # Two ambiguous observations cannot cross either boundary.
    verdict, _ = sprt_bernoulli([1, 0], p0=0.9, p1=0.6, alpha=0.01, beta=0.01)
    assert verdict is SprtVerdict.CONTINUE


def test_sprt_rejects_invalid_hypotheses() -> None:
    with pytest.raises(ValueError):
        sprt_bernoulli([1, 0], p0=0.6, p1=0.9)  # p1 must be < p0
    with pytest.raises(ValueError):
        sprt_bernoulli([1, 0], p0=0.9, p1=0.6, alpha=0.0)  # alpha in (0,1)
    with pytest.raises(ValueError):
        sprt_bernoulli([1, 2], p0=0.9, p1=0.6)  # observations must be 0/1
