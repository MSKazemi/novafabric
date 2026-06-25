from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class JudgeResult(BaseModel):
    judge_id: str
    judge_type: Literal["llm", "embedding", "numerical", "rule"]
    verdict: Literal["pass", "fail", "uncertain"]
    score: float  # 0.0 to 1.0
    rationale: str
    criteria: str  # what was being judged
    actual_output: str
    expected_output: str | None = None
    model_used: str | None = None  # for LLM judge
    latency_ms: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AggregatedJudgment(BaseModel):
    run_id: str
    capsule_id: str | None = None
    judges: list[str]  # judge IDs used
    results: list[JudgeResult]
    consensus_verdict: Literal["pass", "fail", "uncertain"]
    consensus_score: float
    inter_judge_agreement: float | None = None  # Cohen's kappa, None if < 2 judges
    timestamp_utc: str
