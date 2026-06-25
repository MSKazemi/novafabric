from __future__ import annotations

from novafabric.judge.models import AggregatedJudgment, JudgeResult
from novafabric.judge.opa_adapter import judgment_to_rego_input


def _make_judgment(
    verdict: str = "pass",
    score: float = 1.0,
    kappa: float | None = None,
    results: list[JudgeResult] | None = None,
) -> AggregatedJudgment:
    if results is None:
        results = [
            JudgeResult(
                judge_id="j1",
                judge_type="numerical",
                verdict=verdict,  # type: ignore[arg-type]
                score=score,
                rationale="test",
                criteria="test criteria",
                actual_output="output",
            )
        ]
    return AggregatedJudgment(
        run_id="run-1",
        judges=["j1"],
        results=results,
        consensus_verdict=verdict,  # type: ignore[arg-type]
        consensus_score=score,
        inter_judge_agreement=kappa,
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )


class TestJudgmentToRegoInput:
    def test_structure_keys(self) -> None:
        """Output must have top-level 'judgment' key with required subkeys."""
        judgment = _make_judgment("pass", 1.0, kappa=0.9)
        rego = judgment_to_rego_input(judgment)

        assert "judgment" in rego
        j = rego["judgment"]
        assert "consensus_verdict" in j
        assert "consensus_score" in j
        assert "inter_judge_agreement" in j
        assert "judge_count" in j
        assert "all_pass" in j
        assert "any_fail" in j

    def test_pass_verdict_all_pass_true(self) -> None:
        """All results pass → all_pass=True, any_fail=False."""
        judgment = _make_judgment("pass", 1.0)
        rego = judgment_to_rego_input(judgment)["judgment"]
        assert rego["all_pass"] is True
        assert rego["any_fail"] is False

    def test_fail_verdict_any_fail_true(self) -> None:
        """A fail result → all_pass=False, any_fail=True."""
        r_fail = JudgeResult(
            judge_id="j1",
            judge_type="numerical",
            verdict="fail",
            score=0.0,
            rationale="failed",
            criteria="c",
            actual_output="out",
        )
        judgment = _make_judgment("fail", 0.0, results=[r_fail])
        rego = judgment_to_rego_input(judgment)["judgment"]
        assert rego["all_pass"] is False
        assert rego["any_fail"] is True

    def test_kappa_none_passes_through(self) -> None:
        """None kappa → inter_judge_agreement is null/None in output."""
        judgment = _make_judgment("pass", 1.0, kappa=None)
        rego = judgment_to_rego_input(judgment)["judgment"]
        assert rego["inter_judge_agreement"] is None

    def test_kappa_value_preserved(self) -> None:
        """kappa=0.75 → inter_judge_agreement=0.75 in output."""
        judgment = _make_judgment("pass", 1.0, kappa=0.75)
        rego = judgment_to_rego_input(judgment)["judgment"]
        assert rego["inter_judge_agreement"] == 0.75

    def test_judge_count(self) -> None:
        """judge_count equals len(judgment.judges)."""
        judgment = _make_judgment("pass", 1.0)
        rego = judgment_to_rego_input(judgment)["judgment"]
        assert rego["judge_count"] == len(judgment.judges)

    def test_consensus_score_preserved(self) -> None:
        judgment = _make_judgment("fail", 0.3)
        rego = judgment_to_rego_input(judgment)["judgment"]
        assert rego["consensus_score"] == 0.3

    def test_mixed_results_all_pass_false_any_fail_true(self) -> None:
        """Mix of pass and fail results."""
        r_pass = JudgeResult(
            judge_id="j1",
            judge_type="numerical",
            verdict="pass",
            score=1.0,
            rationale="ok",
            criteria="c",
            actual_output="out",
        )
        r_fail = JudgeResult(
            judge_id="j2",
            judge_type="numerical",
            verdict="fail",
            score=0.0,
            rationale="bad",
            criteria="c",
            actual_output="out",
        )
        judgment = AggregatedJudgment(
            run_id="r1",
            judges=["j1", "j2"],
            results=[r_pass, r_fail],
            consensus_verdict="uncertain",
            consensus_score=0.5,
            inter_judge_agreement=None,
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )
        rego = judgment_to_rego_input(judgment)["judgment"]
        assert rego["all_pass"] is False
        assert rego["any_fail"] is True
