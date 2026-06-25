from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from novafabric.judge._llm_judge import LLMJudge, LLMJudgeAPIError
from novafabric.judge.models import JudgeResult


def _mock_response(verdict: str, rationale: str = "test rationale") -> MagicMock:
    """Build a mock httpx.Response that returns a chat-completions JSON body."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"verdict": verdict, "rationale": rationale})
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    return mock_resp


class TestLLMJudgeAPIKeyCheck:
    def test_api_key_not_set_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing API key env var raises LLMJudgeAPIError before any HTTP call."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("TEST_KEY_MISSING", "")
        judge = LLMJudge("criteria", api_key_env="TEST_KEY_MISSING")
        with pytest.raises(LLMJudgeAPIError, match="API key not set"):
            judge.judge("actual output")

    def test_api_key_env_missing_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var not present at all → LLMJudgeAPIError."""
        monkeypatch.delenv("SOME_NONEXISTENT_KEY_XYZ", raising=False)
        judge = LLMJudge("criteria", api_key_env="SOME_NONEXISTENT_KEY_XYZ")
        with pytest.raises(LLMJudgeAPIError):
            judge.judge("actual output")


class TestLLMJudgeAPIFailure:
    def test_api_failure_returns_uncertain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ALL API calls fail, verdict is uncertain and score is 0.5."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        judge = LLMJudge("criteria", k_samples=2)

        with patch("novafabric.judge._llm_judge.httpx.post", side_effect=Exception("connection refused")):
            result = judge.judge("actual output")

        assert result.verdict == "uncertain"
        assert result.score == pytest.approx(0.5)
        assert "API call failed" in result.rationale

    def test_http_error_response_returns_uncertain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 4xx/5xx raises, sample is recorded as uncertain."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        judge = LLMJudge("criteria", k_samples=1)

        mock_resp = MagicMock()
        import httpx
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403 Forbidden", request=MagicMock(), response=MagicMock()
        )

        with patch("novafabric.judge._llm_judge.httpx.post", return_value=mock_resp):
            result = judge.judge("actual output")

        assert result.verdict == "uncertain"


class TestLLMJudgeMajorityVote:
    def test_majority_vote_k3_two_pass_one_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2 pass + 1 fail → final verdict pass, score = 2/3."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        judge = LLMJudge("criteria", k_samples=3)

        responses = [
            _mock_response("pass"),
            _mock_response("pass"),
            _mock_response("fail"),
        ]

        with patch("novafabric.judge._llm_judge.httpx.post", side_effect=responses):
            result = judge.judge("actual output")

        assert result.verdict == "pass"
        assert result.score == pytest.approx(2 / 3)
        assert isinstance(result, JudgeResult)

    def test_majority_vote_all_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All K samples vote fail → final verdict fail, score = 0.0."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        judge = LLMJudge("criteria", k_samples=3)

        responses = [
            _mock_response("fail"),
            _mock_response("fail"),
            _mock_response("fail"),
        ]

        with patch("novafabric.judge._llm_judge.httpx.post", side_effect=responses):
            result = judge.judge("actual output")

        assert result.verdict == "fail"
        assert result.score == pytest.approx(0.0)

    def test_majority_vote_all_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All K samples vote pass → final verdict pass, score = 1.0."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        judge = LLMJudge("criteria", k_samples=3)

        responses = [
            _mock_response("pass"),
            _mock_response("pass"),
            _mock_response("pass"),
        ]

        with patch("novafabric.judge._llm_judge.httpx.post", side_effect=responses):
            result = judge.judge("actual output")

        assert result.verdict == "pass"
        assert result.score == pytest.approx(1.0)

    def test_judge_result_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JudgeResult has model_used and k_samples in metadata."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        judge = LLMJudge("criteria", k_samples=2, model="gpt-4o-mini")

        responses = [_mock_response("pass"), _mock_response("pass")]

        with patch("novafabric.judge._llm_judge.httpx.post", side_effect=responses):
            result = judge.judge("output")

        assert result.model_used == "gpt-4o-mini"
        assert result.metadata["k_samples"] == 2
        assert result.judge_type == "llm"

    def test_fallback_regex_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If LLM returns non-JSON text, regex fallback extracts verdict."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        judge = LLMJudge("criteria", k_samples=1)

        # Return non-JSON content
        payload = {
            "choices": [{"message": {"content": "After evaluation: PASS. Looks good."}}]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status.return_value = None

        with patch("novafabric.judge._llm_judge.httpx.post", return_value=mock_resp):
            result = judge.judge("output")

        assert result.verdict == "pass"

    def test_expected_output_included_in_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When expected output is provided, prompt includes the reference."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        judge = LLMJudge("criteria", k_samples=1)
        prompt = judge._build_prompt("actual", "expected")
        assert "expected" in prompt.lower()
        assert "actual" in prompt.lower()
