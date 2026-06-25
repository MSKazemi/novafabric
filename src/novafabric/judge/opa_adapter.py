from __future__ import annotations

from typing import Any

from .models import AggregatedJudgment


def judgment_to_rego_input(judgment: AggregatedJudgment) -> dict[str, Any]:
    """Convert AggregatedJudgment to OPA input format for policy evaluation.

    The returned dict can be passed directly as the ``input`` document to
    ``opa eval`` or to :class:`novafabric.policy.OpaEngine`.

    Example::

        from novafabric.judge.opa_adapter import judgment_to_rego_input
        from novafabric.policy import get_policy_engine, PolicyInput, PolicySubject, PolicyResource

        rego_input = judgment_to_rego_input(judgment)
        # rego_input["judgment"] contains verdict, score, kappa, etc.
    """
    return {
        "judgment": {
            "consensus_verdict": judgment.consensus_verdict,
            "consensus_score": judgment.consensus_score,
            "inter_judge_agreement": judgment.inter_judge_agreement,
            "judge_count": len(judgment.judges),
            "all_pass": all(r.verdict == "pass" for r in judgment.results),
            "any_fail": any(r.verdict == "fail" for r in judgment.results),
        }
    }
