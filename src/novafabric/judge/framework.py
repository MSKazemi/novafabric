from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from .models import AggregatedJudgment, JudgeResult


def raw_agreement(verdicts: Sequence[str]) -> float | None:
    """Share of the judge pairs that gave the same verdict (None with < 2 judges)."""
    n = len(verdicts)
    if n < 2:
        return None
    counts = Counter(verdicts)
    agreeing = sum(c * (c - 1) for c in counts.values())
    return agreeing / (n * (n - 1))


def aggregate_judgments(
    results: list[JudgeResult],
    run_id: str = "",
    capsule_id: str | None = None,
    judges: list[str] | None = None,
) -> AggregatedJudgment:
    """Aggregate one item's JudgeResults into a single AggregatedJudgment.

    Majority-vote consensus, and the share of judge pairs that agree. A
    chance-corrected kappa needs several items (review eval-adapters-query-20:
    over one item it was 1.0 when the judges agreed and <= 0 otherwise), so
    ``inter_judge_agreement`` stays None here.
    """
    verdicts = [r.verdict for r in results]
    pass_count = verdicts.count("pass")
    fail_count = verdicts.count("fail")

    consensus: Literal["pass", "fail", "uncertain"]
    if pass_count > fail_count:
        consensus = "pass"
    elif fail_count > pass_count:
        consensus = "fail"
    else:
        consensus = "uncertain"

    consensus_score = sum(r.score for r in results) / len(results) if results else 0.5

    return AggregatedJudgment(
        run_id=run_id,
        capsule_id=capsule_id,
        judges=judges or [r.judge_id for r in results],
        results=results,
        consensus_verdict=consensus,
        consensus_score=consensus_score,
        inter_judge_agreement=None,
        raw_agreement=raw_agreement(verdicts),
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )


class JudgeFramework:
    """Orchestrates multiple judges and aggregates results."""

    def __init__(
        self,
        judges: list[Any],
        *,
        kappa_threshold: float = 0.6,
    ) -> None:
        self.judges = judges
        self.kappa_threshold = kappa_threshold

    def evaluate(
        self,
        actual: str,
        expected: str | None = None,
        run_id: str | None = None,
        capsule_id: str | None = None,
    ) -> AggregatedJudgment:
        """Run all judges and aggregate results."""
        results: list[JudgeResult] = []
        for judge in self.judges:
            try:
                result = judge.judge(actual, expected)
                results.append(result)
            except Exception as exc:
                # Fail-open: record uncertain result so pipeline continues
                results.append(
                    JudgeResult(
                        judge_id=type(judge).__name__,
                        judge_type="rule",
                        verdict="uncertain",
                        score=0.5,
                        rationale=f"Judge failed: {exc}",
                        criteria="unknown",
                        actual_output=actual,
                        expected_output=expected,
                    )
                )

        return aggregate_judgments(
            results,
            run_id=run_id or "",
            capsule_id=capsule_id,
            judges=[type(j).__name__ for j in self.judges],
        )

    def check_kappa_gate(self, judgment: AggregatedJudgment) -> bool:
        """True when the judges agree at least ``kappa_threshold`` (for OPA gate integration).

        Uses the kappa when the judgment carries one (several items), else the
        raw pairwise agreement of a single item. A single judge has nothing to
        agree with and passes.
        """
        if judgment.inter_judge_agreement is not None:
            return judgment.inter_judge_agreement >= self.kappa_threshold
        if judgment.raw_agreement is not None:
            return judgment.raw_agreement >= self.kappa_threshold
        return True  # one judge: no agreement requirement
