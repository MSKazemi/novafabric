from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Literal

import httpx

from .models import JudgeResult

_VERDICT_RE = re.compile(r'\b(PASS|FAIL|UNCERTAIN)\b', re.IGNORECASE)

_SYSTEM_PROMPT = """\
You are a strict evaluator. Your task is to judge whether an AI output meets the given criteria.

Respond with a JSON object containing exactly these fields:
{
  "verdict": "pass" | "fail" | "uncertain",
  "rationale": "<one or two sentences explaining your verdict>"
}

Verdict rules:
- "pass": the output clearly meets the criteria
- "fail": the output clearly does not meet the criteria
- "uncertain": you cannot determine with confidence

Do NOT add any text outside the JSON object.
"""


class LLMJudgeAPIError(RuntimeError):
    """Raised when the LLM API call cannot be completed."""


class LLMJudge:
    """Judges outputs using an LLM with self-consistency (K samples).

    Uses httpx to call any OpenAI-compatible API (including local Ollama).
    Supports OpenAI-compatible APIs (including local Ollama).
    """

    def __init__(
        self,
        criteria: str,
        *,
        model: str = "gpt-4o-mini",
        api_base: str = "https://api.openai.com/v1",
        api_key_env: str = "OPENAI_API_KEY",
        k_samples: int = 3,
        pass_threshold: float = 0.6,
        timeout: float = 30.0,
    ) -> None:
        self.criteria = criteria
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.api_key_env = api_key_env
        self.k_samples = k_samples
        self.pass_threshold = pass_threshold
        self.timeout = timeout
        self._judge_id = f"llm-{uuid.uuid4().hex[:8]}"

    def judge(self, actual: str, expected: str | None = None) -> JudgeResult:
        """Evaluate using LLM with self-consistency.

        Makes K API calls to the configured model.
        Verdict is majority vote of K samples.
        Score = fraction of K samples voting "pass".
        """
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise LLMJudgeAPIError(
                f"API key not set: environment variable {self.api_key_env!r} is empty or missing. "
                f"Set it before using LLMJudge."
            )

        t0 = time.monotonic()
        sample_verdicts: list[str] = []
        sample_rationales: list[str] = []

        for _ in range(self.k_samples):
            try:
                verdict, rationale = self._call_once(actual, expected, api_key)
            except Exception as exc:
                # Single sample failure: record uncertain and continue
                sample_verdicts.append("uncertain")
                sample_rationales.append(f"API call failed: {exc}")
                continue
            sample_verdicts.append(verdict)
            sample_rationales.append(rationale)

        pass_count = sample_verdicts.count("pass")
        fail_count = sample_verdicts.count("fail")
        uncertain_count = sample_verdicts.count("uncertain")

        final_verdict: Literal["pass", "fail", "uncertain"]
        if pass_count > fail_count and pass_count > uncertain_count:
            final_score = pass_count / len(sample_verdicts)
            if final_score >= self.pass_threshold:
                final_verdict = "pass"
            else:
                final_verdict = "uncertain"
                final_score = 0.5
        elif fail_count > pass_count and fail_count > uncertain_count:
            final_verdict = "fail"
            final_score = pass_count / len(sample_verdicts)
        else:
            # Tie or all uncertain → uncertain with score 0.5
            final_verdict = "uncertain"
            final_score = 0.5

        score = final_score

        latency_ms = (time.monotonic() - t0) * 1000
        rationale = (
            f"Self-consistency ({self.k_samples} samples): "
            f"pass={pass_count}, fail={fail_count}, "
            f"uncertain={sample_verdicts.count('uncertain')}. "
            f"Rationales: {'; '.join(sample_rationales[:2])}"
        )

        return JudgeResult(
            judge_id=self._judge_id,
            judge_type="llm",
            verdict=final_verdict,
            score=score,
            rationale=rationale,
            criteria=self.criteria,
            actual_output=actual,
            expected_output=expected,
            model_used=self.model,
            latency_ms=round(latency_ms, 3),
            metadata={
                "k_samples": self.k_samples,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "sample_verdicts": sample_verdicts,
            },
        )

    def _call_once(
        self, actual: str, expected: str | None, api_key: str
    ) -> tuple[str, str]:
        """Single LLM evaluation call. Returns (verdict, rationale)."""
        prompt = self._build_prompt(actual, expected)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        response = httpx.post(
            f"{self.api_base}/chat/completions",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        content: str = data["choices"][0]["message"]["content"]

        # Try to parse JSON response
        try:
            parsed = json.loads(content)
            raw_verdict = str(parsed.get("verdict", "uncertain")).lower()
            rationale = str(parsed.get("rationale", content))
        except (json.JSONDecodeError, KeyError):
            # Fallback: regex extraction
            m = _VERDICT_RE.search(content)
            raw_verdict = m.group(1).lower() if m else "uncertain"
            rationale = content[:500]

        # Normalise to allowed values
        if raw_verdict not in ("pass", "fail", "uncertain"):
            raw_verdict = "uncertain"

        return raw_verdict, rationale

    def _build_prompt(self, actual: str, expected: str | None) -> str:
        """Build evaluation prompt. Must produce parseable verdict."""
        lines = [
            f"Criteria: {self.criteria}",
            "",
            f"Actual output:\n{actual}",
        ]
        if expected is not None:
            lines += ["", f"Expected output (reference):\n{expected}"]
        lines += [
            "",
            "Evaluate whether the actual output meets the criteria.",
        ]
        return "\n".join(lines)
