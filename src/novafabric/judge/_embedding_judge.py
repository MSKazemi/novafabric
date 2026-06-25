from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from .models import JudgeResult

if TYPE_CHECKING:
    pass  # nothing to import at type-check time; sentence-transformers is optional


class EmbeddingJudge:
    """Judges outputs by semantic similarity using embeddings.

    Requires sentence-transformers (optional).
    Falls back to simple token overlap (Jaccard) if sentence-transformers not installed.
    """

    def __init__(
        self,
        criteria: str,
        *,
        similarity_threshold: float = 0.8,
        model: str = "all-MiniLM-L6-v2",
    ) -> None:
        self.criteria = criteria
        self.similarity_threshold = similarity_threshold
        self.model = model
        self._judge_id = f"embedding-{uuid.uuid4().hex[:8]}"
        self._encoder: Any = None  # SentenceTransformer instance or None
        self._use_transformers: bool | None = None  # lazy-resolved on first call

    def _resolve_encoder(self) -> bool:
        """Try to load sentence-transformers. Returns True if available."""
        if self._use_transformers is not None:
            return self._use_transformers
        try:
            import importlib

            st = importlib.import_module("sentence_transformers")
            self._encoder = st.SentenceTransformer(self.model)
            self._use_transformers = True
        except (ImportError, ModuleNotFoundError):
            self._use_transformers = False
        # _use_transformers is always bool after the try/except above
        return bool(self._use_transformers)

    def judge(self, actual: str, expected: str) -> JudgeResult:
        """Compare actual to expected via cosine similarity."""
        t0 = time.monotonic()

        use_transformers = self._resolve_encoder()

        if use_transformers and self._encoder is not None:
            similarity = self._cosine_similarity(actual, expected)
            method = f"sentence-transformers ({self.model})"
        else:
            similarity = self._jaccard_similarity(actual, expected)
            method = "token-jaccard (fallback)"

        verdict: Literal["pass", "fail", "uncertain"]
        if similarity >= self.similarity_threshold:
            verdict = "pass"
        else:
            verdict = "fail"

        latency_ms = (time.monotonic() - t0) * 1000

        return JudgeResult(
            judge_id=self._judge_id,
            judge_type="embedding",
            verdict=verdict,
            score=float(similarity),
            rationale=(
                f"similarity={similarity:.4f} via {method}; "
                f"threshold={self.similarity_threshold}"
            ),
            criteria=self.criteria,
            actual_output=actual,
            expected_output=expected,
            latency_ms=round(latency_ms, 3),
            metadata={"method": method},
        )

    def _cosine_similarity(self, a: str, b: str) -> float:
        """Encode both strings and compute cosine similarity.

        Uses numpy when available; falls back to pure-Python otherwise.
        """
        assert self._encoder is not None
        vecs: Any = self._encoder.encode([a, b])
        va: Any = vecs[0]
        vb: Any = vecs[1]

        try:
            # numpy is a sentence-transformers dependency; stubs may not be installed
            import numpy as np

            norm_a = float(np.linalg.norm(va))
            norm_b = float(np.linalg.norm(vb))
            if norm_a == 0.0 or norm_b == 0.0:
                return 1.0 if norm_a == norm_b else 0.0
            return float(np.dot(va, vb) / (norm_a * norm_b))
        except (ImportError, ModuleNotFoundError):
            # Pure-Python fallback for environments without numpy (e.g. test mocks)
            return self._cosine_pure_python(va, vb)

    @staticmethod
    def _cosine_pure_python(va: Any, vb: Any) -> float:
        """Pure-Python cosine similarity for list-like vectors."""
        pairs: list[tuple[float, float]] = [(float(x), float(y)) for x, y in zip(va, vb)]
        dot: float = sum(x * y for x, y in pairs)
        norm_a: float = sum(float(x) ** 2 for x in va) ** 0.5
        norm_b: float = sum(float(y) ** 2 for y in vb) ** 0.5
        if norm_a == 0.0 or norm_b == 0.0:
            return 1.0 if norm_a == norm_b else 0.0
        return dot / (norm_a * norm_b)

    def _jaccard_similarity(self, a: str, b: str) -> float:
        """Token-level Jaccard similarity as fallback."""
        tokens_a = set(a.lower().split())
        tokens_b = set(b.lower().split())
        if not tokens_a and not tokens_b:
            return 1.0
        return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
