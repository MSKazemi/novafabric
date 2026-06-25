from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from novafabric.judge._embedding_judge import EmbeddingJudge
from novafabric.judge.models import JudgeResult


class TestEmbeddingJudgeJaccard:
    """Tests that run without sentence-transformers (Jaccard fallback path)."""

    def _judge_with_jaccard(self, criteria: str, **kwargs: object) -> EmbeddingJudge:
        """Create an EmbeddingJudge forced into Jaccard fallback mode."""
        j = EmbeddingJudge(criteria, **kwargs)  # type: ignore[arg-type]
        j._use_transformers = False  # force fallback
        return j

    def test_identical_strings_similarity_1(self) -> None:
        judge = self._judge_with_jaccard("same text", similarity_threshold=0.8)
        result = judge.judge("hello world", "hello world")
        assert result.score == pytest.approx(1.0)
        assert result.verdict == "pass"

    def test_completely_different_strings(self) -> None:
        judge = self._judge_with_jaccard("different text", similarity_threshold=0.5)
        result = judge.judge("apple orange", "car truck train")
        assert result.score == pytest.approx(0.0)
        assert result.verdict == "fail"

    def test_partial_overlap(self) -> None:
        judge = self._judge_with_jaccard("partial overlap", similarity_threshold=0.2)
        # "hello world" vs "hello there" → intersection={"hello"}, union={"hello","world","there"}
        result = judge.judge("hello world", "hello there")
        expected_jaccard = 1 / 3
        assert result.score == pytest.approx(expected_jaccard, abs=1e-6)

    def test_verdict_based_on_threshold_pass(self) -> None:
        judge = self._judge_with_jaccard("threshold test", similarity_threshold=0.3)
        result = judge.judge("the quick brown fox", "the quick brown dog")
        # intersection={"the","quick","brown"}, union={"the","quick","brown","fox","dog"}
        expected = 3 / 5
        assert result.score == pytest.approx(expected, abs=1e-6)
        assert result.verdict == "pass"

    def test_verdict_based_on_threshold_fail(self) -> None:
        judge = self._judge_with_jaccard("threshold test", similarity_threshold=0.9)
        result = judge.judge("hello world", "hello there")
        assert result.verdict == "fail"

    def test_empty_strings_both_empty(self) -> None:
        judge = self._judge_with_jaccard("empty test", similarity_threshold=0.5)
        result = judge.judge("", "")
        # Both empty → Jaccard = 1.0
        assert result.score == pytest.approx(1.0)

    def test_judge_result_fields(self) -> None:
        judge = self._judge_with_jaccard("criteria", similarity_threshold=0.8)
        result = judge.judge("hello", "hello")
        assert isinstance(result, JudgeResult)
        assert result.judge_type == "embedding"
        assert result.actual_output == "hello"
        assert result.expected_output == "hello"
        assert result.latency_ms is not None
        assert "fallback" in result.rationale or "jaccard" in result.rationale.lower()


class TestEmbeddingJudgeSentenceTransformers:
    """Tests for the sentence-transformers code path (mocked)."""

    def test_jaccard_fallback_when_import_fails(self) -> None:
        """If sentence-transformers is not installed, falls back to Jaccard."""
        judge = EmbeddingJudge("test", similarity_threshold=0.8)

        with patch.dict("sys.modules", {"sentence_transformers": None}):
            # Reset lazy state
            judge._use_transformers = None
            judge._encoder = None
            # When import fails, _resolve_encoder returns False
            with patch("builtins.__import__", side_effect=ImportError("no module")):
                judge._use_transformers = False  # simulate the import failure result

        result = judge.judge("hello world", "hello world")
        # Should still work via Jaccard
        assert result.score == pytest.approx(1.0)

    def test_sentence_transformers_path(self) -> None:
        """Mock sentence-transformers to return controlled embeddings (pure Python lists)."""
        mock_model = MagicMock()
        # va and vb identical → cosine = 1.0
        vec = [1.0, 0.0, 0.0]
        mock_model.encode.return_value = [vec, vec]

        judge = EmbeddingJudge("criteria", similarity_threshold=0.8)
        judge._use_transformers = True
        judge._encoder = mock_model

        result = judge.judge("same text", "same text")
        assert result.score == pytest.approx(1.0)
        assert result.verdict == "pass"

    def test_sentence_transformers_dissimilar(self) -> None:
        """Mock orthogonal vectors → cosine = 0.0 (pure Python lists)."""
        mock_model = MagicMock()
        va = [1.0, 0.0]
        vb = [0.0, 1.0]
        mock_model.encode.return_value = [va, vb]

        judge = EmbeddingJudge("criteria", similarity_threshold=0.5)
        judge._use_transformers = True
        judge._encoder = mock_model

        result = judge.judge("hello", "world")
        assert result.score == pytest.approx(0.0)
        assert result.verdict == "fail"
