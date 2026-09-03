"""Production regression alarm — ADR-0147 D4 / NF-156.

Two tests here carry the design's whole justification:

- `test_a_single_dip_does_not_fire` — the ADR's stated reason for reusing the SPRT
  rather than thresholding a delta. If this passes trivially the alarm is a delta
  gate wearing statistical clothing.
- `test_drift_polarity_must_be_declared` — `significance_diff` reads 1 as *pass*,
  but the ADR offers the window as "drifted/not (or pass/fail)", which are opposite.
  Getting this wrong makes the alarm fire on improvement and stay silent on
  regression, with every number still looking plausible.
"""

from __future__ import annotations

import json

import pytest

from novafabric.assure.alarm import (
    AlarmError,
    AlarmVerdict,
    RegressionAlarm,
    attach_facet,
    evaluate,
    facet_from_capsule,
)

HEALTHY = [1] * 40
DEGRADED = [0] * 40


# ── the three-valued verdict ─────────────────────────────────────────────────


def test_a_sustained_drop_is_a_regression_and_fires() -> None:
    alarm = evaluate(HEALTHY, DEGRADED)

    assert alarm.verdict is AlarmVerdict.regression
    assert alarm.fired is True


def test_a_healthy_window_does_not_fire() -> None:
    alarm = evaluate(HEALTHY, HEALTHY)

    assert alarm.verdict is not AlarmVerdict.regression
    assert alarm.fired is False


def test_a_single_dip_does_not_fire() -> None:
    """The ADR's reason for reusing the SPRT: one bad run is noise, not a regression."""
    window = [1] * 39 + [0]

    alarm = evaluate(HEALTHY, window)

    assert alarm.fired is False, "a single-run dip must not fire an alarm"
    assert alarm.verdict is not AlarmVerdict.regression


def test_inconclusive_does_not_fire() -> None:
    """`CONTINUE` means 'not enough evidence', which is not a regression.

    Treating undecided as regression is exactly the false alarm the SPRT prevents.
    """
    alarm = evaluate([1, 1, 1], [1, 0])

    if alarm.verdict is AlarmVerdict.inconclusive:
        assert alarm.fired is False
    # If the primitive decided on this tiny sample, the invariant below still holds.
    assert alarm.fired is (alarm.verdict is AlarmVerdict.regression)


def test_fired_is_true_only_for_regression() -> None:
    for baseline, window in ((HEALTHY, DEGRADED), (HEALTHY, HEALTHY),
                             (HEALTHY, [1] * 39 + [0]), ([1, 1, 1], [1, 0])):
        alarm = evaluate(baseline, window)
        assert alarm.fired is (alarm.verdict is AlarmVerdict.regression)


# ── polarity ─────────────────────────────────────────────────────────────────


def test_drift_polarity_must_be_declared() -> None:
    """Drift flags are 1 = *bad*; feeding them raw inverts the alarm silently."""
    drifted_window = [1] * 40      # every run drifted -> a real regression
    clean_baseline = [0] * 40      # nothing drifted

    wrong = evaluate(clean_baseline, drifted_window)
    right = evaluate(clean_baseline, drifted_window, outcomes_are_drift_flags=True)

    assert right.verdict is AlarmVerdict.regression, (
        "an all-drifted window is a regression once polarity is declared"
    )
    assert wrong.verdict is not right.verdict, (
        "the flag must change the verdict — otherwise it is decorative"
    )


def test_the_inversion_is_symmetric() -> None:
    healthy_as_drift_flags = [0] * 40
    alarm = evaluate([0] * 40, healthy_as_drift_flags, outcomes_are_drift_flags=True)
    assert alarm.fired is False


# ── it reuses the primitive ──────────────────────────────────────────────────


def test_it_calls_the_shipped_sprt_primitive(monkeypatch) -> None:
    """ADR-0147 D4: reuse the ADR-0080 primitive, do not re-implement it."""
    import novafabric.assure.alarm as mod

    called: dict[str, bool] = {}
    real = mod.significance_diff

    def spy(*args, **kwargs):
        called["yes"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "significance_diff", spy)
    evaluate(HEALTHY, DEGRADED)

    assert called.get("yes"), "the alarm must call significance_diff"


def test_the_alarm_records_the_parameters_it_ran_under() -> None:
    """The same window judged under different p0/p1 is a different verdict."""
    alarm = evaluate(HEALTHY, DEGRADED)

    for key in ("p0", "p1", "alpha", "beta", "verdict"):
        assert key in alarm.sprt, f"sprt block must record {key}"


def test_sprt_parameters_can_be_tuned_through() -> None:
    alarm = evaluate(HEALTHY, DEGRADED, p0=0.9, p1=0.5)
    assert alarm.sprt["p0"] == 0.9
    assert alarm.sprt["p1"] == 0.5


# ── it is not the promote gate ───────────────────────────────────────────────


def test_the_alarm_does_not_carry_the_promote_gate_exit_code() -> None:
    """ADR-0147 D4: a standing alarm, not a second promote gate (ADR-0080 unchanged)."""
    alarm = evaluate(HEALTHY, DEGRADED)
    payload = alarm.model_dump()

    assert "exit_code" not in payload
    assert not hasattr(alarm, "exit_code"), (
        "SignificanceDiff.exit_code() returns 3 on a regression — that is ADR-0080's "
        "promote-gate contract, and an alarm carrying it would be a second gate"
    )


# ── input validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [[], [1, 2], [0, -1], [0.5]])
def test_a_non_bernoulli_sequence_is_refused(bad: list) -> None:
    with pytest.raises(AlarmError):
        evaluate(HEALTHY, bad)


def test_an_empty_baseline_is_refused() -> None:
    with pytest.raises(AlarmError, match="baseline_outcomes"):
        evaluate([], HEALTHY)


# ── facet ────────────────────────────────────────────────────────────────────


def test_no_alarm_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r", "facets": {"other": {"x": 1}}}
    original = json.dumps(capsule, sort_keys=True)

    assert attach_facet(capsule, None) == capsule
    assert json.dumps(capsule, sort_keys=True) == original


def test_round_trip_through_a_capsule() -> None:
    alarm = evaluate(HEALTHY, DEGRADED)
    assert facet_from_capsule(attach_facet({"run_id": "r"}, alarm)) == alarm


def test_an_invalid_alarm_is_reported() -> None:
    with pytest.raises(AlarmError, match="invalid regression alarm"):
        facet_from_capsule({"facets": {"regression_alarm": {"metric": "m"}}})


def test_the_model_is_frozen() -> None:
    alarm = evaluate(HEALTHY, DEGRADED)
    with pytest.raises(Exception):
        alarm.fired = False  # type: ignore[misc]


def test_it_is_a_regression_alarm_model() -> None:
    assert isinstance(evaluate(HEALTHY, DEGRADED), RegressionAlarm)
