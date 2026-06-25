from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from ._kappa import compute_kappa
from .models import AggregatedJudgment, JudgeResult


def aggregate_judgments(
    results: list[JudgeResult],
    run_id: str = "",
    capsule_id: str | None = None,
    judges: list[str] | None = None,
) -> AggregatedJudgment:
    """Aggregate a list of JudgeResults into a single AggregatedJudgment.

    Computes majority-vote consensus and inter-judge kappa when >= 2 results.
    """
    kappa: float | None = None
    if len(results) >= 2:
        # Each result is one judge's verdict for a single item.
        # kappa for a single item with multiple raters:
        # judgments = [list of all verdicts for this item]
        kappa = compute_kappa([[r.verdict for r in results]])

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
        inter_judge_agreement=kappa,
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

        kappa: float | None = None
        if len(results) >= 2:
            kappa = compute_kappa([[r.verdict for r in results]])

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
            run_id=run_id or "",
            capsule_id=capsule_id,
            judges=[type(j).__name__ for j in self.judges],
            results=results,
            consensus_verdict=consensus,
            consensus_score=consensus_score,
            inter_judge_agreement=kappa,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )

    def check_kappa_gate(self, judgment: AggregatedJudgment) -> bool:
        """Returns True if kappa >= threshold (for OPA gate integration).

        Single-judge evaluations always pass the gate (no kappa to check).
        """
        if judgment.inter_judge_agreement is None:
            return True  # single judge, no kappa requirement
        return judgment.inter_judge_agreement >= self.kappa_threshold
