from __future__ import annotations

import pytest

from novafabric.judge._numerical_judge import NumericalJudge
from novafabric.judge.models import JudgeResult


class TestNumericalJudge:
    def test_exact_match_pass(self) -> None:
        judge = NumericalJudge("output must be 'hello'", exact_match="hello")
        result = judge.judge("hello")
        assert result.verdict == "pass"
        assert result.score == pytest.approx(1.0)

    def test_exact_match_fail(self) -> None:
        judge = NumericalJudge("output must be 'hello'", exact_match="hello")
        result = judge.judge("world")
        assert result.verdict == "fail"
        assert result.score == pytest.approx(0.0)

    def test_regex_match_pass(self) -> None:
        judge = NumericalJudge("output must contain a number", regex_pattern=r"\d+")
        result = judge.judge("The answer is 42")
        assert result.verdict == "pass"
        assert result.score == pytest.approx(1.0)

    def test_regex_match_fail(self) -> None:
        judge = NumericalJudge("output must contain a number", regex_pattern=r"\d+")
        result = judge.judge("No numbers here")
        assert result.verdict == "fail"
        assert result.score == pytest.approx(0.0)

    def test_numeric_range_pass(self) -> None:
        judge = NumericalJudge("output must be in [0, 100]", numeric_range=(0.0, 100.0))
        result = judge.judge("42.5")
        assert result.verdict == "pass"
        assert result.score == pytest.approx(1.0)

    def test_numeric_range_fail_above(self) -> None:
        judge = NumericalJudge("output must be in [0, 100]", numeric_range=(0.0, 100.0))
        result = judge.judge("150")
        assert result.verdict == "fail"
        assert result.score == pytest.approx(0.0)

    def test_numeric_range_fail_below(self) -> None:
        judge = NumericalJudge("output must be in [0, 100]", numeric_range=(0.0, 100.0))
        result = judge.judge("-5")
        assert result.verdict == "fail"
        assert result.score == pytest.approx(0.0)

    def test_numeric_range_not_parseable(self) -> None:
        judge = NumericalJudge("output must be a number", numeric_range=(0.0, 100.0))
        result = judge.judge("not a number")
        assert result.verdict == "fail"
        assert result.score == pytest.approx(0.0)
        assert "not parseable" in result.rationale

    def test_length_range_pass(self) -> None:
        judge = NumericalJudge("output length must be 5-10", length_range=(5, 10))
        result = judge.judge("hello")  # length=5
        assert result.verdict == "pass"
        assert result.score == pytest.approx(1.0)

    def test_length_range_fail_too_short(self) -> None:
        judge = NumericalJudge("output length must be 5-10", length_range=(5, 10))
        result = judge.judge("hi")  # length=2
        assert result.verdict == "fail"

    def test_length_range_fail_too_long(self) -> None:
        judge = NumericalJudge("output length must be 5-10", length_range=(5, 10))
        result = judge.judge("this is way too long for the constraint")
        assert result.verdict == "fail"

    def test_no_criteria_returns_uncertain(self) -> None:
        judge = NumericalJudge("no criteria configured")
        result = judge.judge("anything")
        assert result.verdict == "uncertain"
        assert result.score == pytest.approx(0.5)

    def test_combined_criteria_mixed_result(self) -> None:
        """One criterion passes, one fails → average score below threshold."""
        judge = NumericalJudge(
            "exact match and length",
            exact_match="hello",
            length_range=(10, 20),  # fails for "hello" (length=5)
            pass_threshold=0.75,
        )
        result = judge.judge("hello")
        # exact_match=pass (1.0), length_range=fail (0.0) → avg=0.5 < 0.75
        assert result.verdict == "fail"
        assert result.score == pytest.approx(0.5)

    def test_judge_result_fields(self) -> None:
        judge = NumericalJudge("criteria", exact_match="ok")
        result = judge.judge("ok", expected="ok")
        assert isinstance(result, JudgeResult)
        assert result.judge_type == "numerical"
        assert result.criteria == "criteria"
        assert result.actual_output == "ok"
        assert result.expected_output == "ok"
        assert result.latency_ms is not None
        assert result.latency_ms >= 0.0

    def test_regex_compiled_at_init(self) -> None:
        """Invalid regex raises at construction time."""
        with pytest.raises(Exception):
            NumericalJudge("bad regex", regex_pattern="[unclosed")
