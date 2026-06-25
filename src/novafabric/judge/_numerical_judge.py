from __future__ import annotations

import re
import time
import uuid
from typing import Literal

from .models import JudgeResult


class NumericalJudge:
    """Judges outputs using numerical metrics (exact match, regex, length, numeric range)."""

    def __init__(
        self,
        criteria: str,
        *,
        exact_match: str | None = None,
        regex_pattern: str | None = None,
        numeric_range: tuple[float, float] | None = None,
        length_range: tuple[int, int] | None = None,
        pass_threshold: float = 0.5,
    ) -> None:
        self.criteria = criteria
        self.exact_match = exact_match
        self.regex_pattern = regex_pattern
        self.numeric_range = numeric_range
        self.length_range = length_range
        self.pass_threshold = pass_threshold
        self._judge_id = f"numerical-{uuid.uuid4().hex[:8]}"

        if regex_pattern is not None:
            # Compile immediately to surface regex errors at construction time
            self._compiled_regex: re.Pattern[str] | None = re.compile(regex_pattern)
        else:
            self._compiled_regex = None

    def judge(self, actual: str, expected: str | None = None) -> JudgeResult:
        """Evaluate actual output against configured criteria."""
        t0 = time.monotonic()
        scores: list[float] = []
        rationale_parts: list[str] = []
        verdict: Literal["pass", "fail", "uncertain"] = "uncertain"

        if self.exact_match is not None:
            match = actual == self.exact_match
            scores.append(1.0 if match else 0.0)
            rationale_parts.append(
                f"exact_match={'yes' if match else 'no'} "
                f"(expected={self.exact_match!r}, got={actual!r})"
            )

        if self._compiled_regex is not None:
            found = bool(self._compiled_regex.search(actual))
            scores.append(1.0 if found else 0.0)
            rationale_parts.append(
                f"regex_match={'yes' if found else 'no'} "
                f"(pattern={self.regex_pattern!r})"
            )

        if self.numeric_range is not None:
            lo, hi = self.numeric_range
            try:
                val = float(actual.strip())
                in_range = lo <= val <= hi
                # Score: 1.0 if in range, 0.0 otherwise
                scores.append(1.0 if in_range else 0.0)
                rationale_parts.append(
                    f"numeric_range={'in range' if in_range else 'out of range'} "
                    f"({val} vs [{lo}, {hi}])"
                )
            except ValueError:
                scores.append(0.0)
                rationale_parts.append(
                    f"numeric_range=not parseable (got={actual!r})"
                )

        if self.length_range is not None:
            lo_len, hi_len = self.length_range
            length = len(actual)
            in_range = lo_len <= length <= hi_len
            scores.append(1.0 if in_range else 0.0)
            rationale_parts.append(
                f"length_range={'in range' if in_range else 'out of range'} "
                f"(len={length} vs [{lo_len}, {hi_len}])"
            )

        if not scores:
            # No criteria configured — uncertain
            final_score = 0.5
            verdict = "uncertain"
            rationale = "No criteria configured"
        else:
            final_score = sum(scores) / len(scores)
            if final_score >= self.pass_threshold:
                verdict = "pass"
            else:
                verdict = "fail"
            rationale = "; ".join(rationale_parts)

        latency_ms = (time.monotonic() - t0) * 1000

        return JudgeResult(
            judge_id=self._judge_id,
            judge_type="numerical",
            verdict=verdict,
            score=final_score,
            rationale=rationale,
            criteria=self.criteria,
            actual_output=actual,
            expected_output=expected,
            latency_ms=round(latency_ms, 3),
        )
