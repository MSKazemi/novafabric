from __future__ import annotations

import pytest

from novafabric.judge._numerical_judge import NumericalJudge
from novafabric.judge.framework import JudgeFramework, aggregate_judgments
from novafabric.judge.models import AggregatedJudgment, JudgeResult


def _make_result(verdict: str, score: float, criteria: str = "test") -> JudgeResult:
    return JudgeResult(
        judge_id=f"judge-{verdict}",
        judge_type="numerical",
        verdict=verdict,  # type: ignore[arg-type]
        score=score,
        rationale=f"test {verdict}",
        criteria=criteria,
        actual_output="output",
    )


class TestJudgeFramework:
    def test_single_judge_no_kappa(self) -> None:
        """Single judge → no kappa (inter_judge_agreement is None)."""
        judge = NumericalJudge("pass if 'ok'", exact_match="ok")
        framework = JudgeFramework([judge])
        result = framework.evaluate("ok")

        assert result.inter_judge_agreement is None
        assert result.consensus_verdict == "pass"

    def test_two_judges_compute_kappa(self) -> None:
        """Two judges → kappa is computed."""
        j1 = NumericalJudge("exact match ok", exact_match="hello")
        j2 = NumericalJudge("length 5+", length_range=(5, 100))
        framework = JudgeFramework([j1, j2])
        result = framework.evaluate("hello")

        assert result.inter_judge_agreement is not None
        assert isinstance(result.inter_judge_agreement, float)

    def test_consensus_all_pass(self) -> None:
        """All judges pass → consensus pass."""
        j1 = NumericalJudge("c1", exact_match="x")
        j2 = NumericalJudge("c2", exact_match="x")
        framework = JudgeFramework([j1, j2])
        result = framework.evaluate("x")
        assert result.consensus_verdict == "pass"

    def test_consensus_majority_fail(self) -> None:
        """Two fail vs one pass → consensus fail."""
        j1 = NumericalJudge("c1", exact_match="pass_value")
        j2 = NumericalJudge("c2", exact_match="other")
        j3 = NumericalJudge("c3", exact_match="other")
        framework = JudgeFramework([j1, j2, j3])
        result = framework.evaluate("pass_value")
        # j1=pass, j2=fail, j3=fail → fail
        assert result.consensus_verdict == "fail"

    def test_judge_failure_fail_open(self) -> None:
        """A judge that raises → uncertain result added, framework continues."""

        class BrokenJudge:
            def judge(self, actual: str, expected: str | None = None) -> JudgeResult:
                raise RuntimeError("simulated judge failure")

        framework = JudgeFramework([BrokenJudge()])  # type: ignore[list-item]
        result = framework.evaluate("anything")

        assert len(result.results) == 1
        assert result.results[0].verdict == "uncertain"
        assert "Judge failed" in result.results[0].rationale

    def test_judge_failure_and_passing_judge(self) -> None:
        """One broken judge + one passing judge → consensus derived from passing judge."""

        class BrokenJudge:
            def judge(self, actual: str, expected: str | None = None) -> JudgeResult:
                raise ValueError("broken")

        j_pass = NumericalJudge("c", exact_match="ok")
        framework = JudgeFramework([BrokenJudge(), j_pass])  # type: ignore[list-item]
        result = framework.evaluate("ok")

        # broken=uncertain, j_pass=pass → pass_count=1, fail_count=0 → pass
        assert result.consensus_verdict == "pass"
        assert len(result.results) == 2

    def test_kappa_gate_above_threshold(self) -> None:
        """kappa >= threshold → check_kappa_gate returns True."""
        j1 = NumericalJudge("c", exact_match="ok")
        j2 = NumericalJudge("c", exact_match="ok")
        framework = JudgeFramework([j1, j2], kappa_threshold=0.6)
        result = framework.evaluate("ok")
        # Both agree perfectly → kappa=1.0
        assert framework.check_kappa_gate(result) is True

    def test_kappa_gate_below_threshold(self) -> None:
        """kappa < threshold → check_kappa_gate returns False."""
        framework = JudgeFramework([], kappa_threshold=0.6)
        # Construct a judgment with low kappa manually
        judgment = AggregatedJudgment(
            run_id="r1",
            judges=["j1", "j2"],
            results=[],
            consensus_verdict="uncertain",
            consensus_score=0.5,
            inter_judge_agreement=0.3,  # below threshold
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )
        assert framework.check_kappa_gate(judgment) is False

    def test_kappa_gate_none_kappa(self) -> None:
        """None kappa (single judge) → gate always passes."""
        framework = JudgeFramework([], kappa_threshold=0.6)
        judgment = AggregatedJudgment(
            run_id="r1",
            judges=["j1"],
            results=[],
            consensus_verdict="pass",
            consensus_score=1.0,
            inter_judge_agreement=None,
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )
        assert framework.check_kappa_gate(judgment) is True

    def test_evaluate_returns_aggregated_judgment(self) -> None:
        j = NumericalJudge("c", exact_match="ok")
        framework = JudgeFramework([j])
        result = framework.evaluate("ok", run_id="run-1", capsule_id="cap-1")

        assert isinstance(result, AggregatedJudgment)
        assert result.run_id == "run-1"
        assert result.capsule_id == "cap-1"
        assert result.timestamp_utc  # non-empty

    def test_consensus_score_is_average(self) -> None:
        """consensus_score = average of individual scores."""
        j1 = NumericalJudge("c", exact_match="ok")
        j2 = NumericalJudge("c", exact_match="not_ok")
        framework = JudgeFramework([j1, j2])
        result = framework.evaluate("ok")
        # j1 score=1.0, j2 score=0.0 → avg=0.5
        assert result.consensus_score == pytest.approx(0.5)

    def test_empty_judges_list(self) -> None:
        """No judges → uncertain, score 0.5, no kappa."""
        framework = JudgeFramework([])
        result = framework.evaluate("anything")
        assert result.consensus_verdict == "uncertain"
        assert result.consensus_score == pytest.approx(0.5)
        assert result.inter_judge_agreement is None


class TestAggregateJudgments:
    def test_aggregate_single_result(self) -> None:
        r = _make_result("pass", 1.0)
        judgment = aggregate_judgments([r], run_id="r1")
        assert judgment.consensus_verdict == "pass"
        assert judgment.inter_judge_agreement is None  # single judge

    def test_aggregate_two_results_kappa(self) -> None:
        r1 = _make_result("pass", 1.0)
        r2 = _make_result("fail", 0.0)
        judgment = aggregate_judgments([r1, r2])
        # tie → uncertain, kappa computed
        assert judgment.consensus_verdict == "uncertain"
        assert judgment.inter_judge_agreement is not None

    def test_aggregate_majority_pass(self) -> None:
        results = [
            _make_result("pass", 1.0),
            _make_result("pass", 0.9),
            _make_result("fail", 0.0),
        ]
        judgment = aggregate_judgments(results)
        assert judgment.consensus_verdict == "pass"
