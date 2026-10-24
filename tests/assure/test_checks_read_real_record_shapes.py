"""``nova assure`` LLM02/LLM04/LLM07 read the fields capsules really carry (review misc-3-02).

All three read keys no writer produces — ``secrets_found``, ``tokens.input``, a
``level: DEBUG`` record with a ``system_prompt`` key — so they PASSed for every
capsule. These fixtures are validated against the shipped schemas, so a check that
drifts from the real record shape fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.assure.checks import LLM02Check, LLM04Check, LLM07Check

SCHEMAS = Path(__file__).resolve().parents[2] / "src" / "novafabric" / "schemas"


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS / name).read_text())
    return loaded


def _scanned_proof(cap: Path, secrets: int) -> dict[str, Any]:
    """The proof the real secret scanner writes for *secrets* planted AWS keys."""
    from novafabric.capture.secrets import SecretScannerV0

    lines = [json.dumps({"note": f"key sk-ant-{'Q' * 30}{i}"}) for i in range(secrets)]
    (cap / "tool-calls.jsonl").write_text("".join(line + "\n" for line in lines) or "")
    proof: dict[str, Any] = SecretScannerV0(capsule_dir=cap, run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX").scan_and_redact()
    return proof


def _unsafe_skip() -> dict[str, Any]:
    return {
        "finding_id": "01HXAY7M5JZ8R7K4P9DPBYK2WX", "rule_id": "aws-access-key",
        "rationale": "test fixture", "decided_by": "interactive-cli",
        "decided_at": "2026-10-24T00:00:00Z",
    }


def _call(
    *, input_tokens: int = 10, system: str | None = None, reply: str = "ok",
    log_level: str | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": "hello"})
    record: dict[str, Any] = {
        "schema_version": "0.1.0", "semconv_version": "1.37.0", "model_call_id": "01HXAY7M5JZ8R7K4P9DPBYK2WX",
        "parent_span_id": "0123456789abcdef", "started_at": "2026-10-24T00:00:00Z",
        "finished_at": "2026-10-24T00:00:01Z", "duration_ms": 1000,
        "gen_ai.system": "openai", "gen_ai.request.model": "gpt-x",
        "gen_ai.response.model": "gpt-x", "gen_ai.operation.name": "chat",
        "gen_ai.request.messages": messages,
        "gen_ai.response.choices": [
            {"index": 0, "message": {"role": "assistant", "content": reply},
             "finish_reason": "stop"},
        ],
        "gen_ai.usage.input_tokens": input_tokens, "gen_ai.usage.output_tokens": 1,
        "status": "success",
    }
    if log_level is not None:
        record["log_level"] = log_level
    return record


def _capsule(tmp_path: Path, *, secrets: int | None = None, unsafe_skip: bool = False,
             calls: list[dict[str, Any]] | None = None) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir()
    if secrets is not None:
        proof = _scanned_proof(cap, secrets)
        assert proof["findings_count"]["total"] == secrets
        if unsafe_skip:
            proof["unsafe_skips"] = [_unsafe_skip()]
        jsonschema.validate(proof, _schema("secret-redaction.schema.json"))
        (cap / "redaction-proof.json").write_text(json.dumps(proof))
    if calls is not None:
        model_call = _schema("model-call.schema.json")
        for call in calls:
            jsonschema.validate(call, model_call)
        (cap / "model-calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
    return cap


# ------------------------------------------------------------------- LLM02


def test_llm02_fails_when_the_proof_records_findings(tmp_path: Path) -> None:
    result = LLM02Check().run(_capsule(tmp_path, secrets=3))
    assert result.status == "FAIL"
    assert "3" in result.message


def test_llm02_fails_when_the_scanner_skipped_a_file(tmp_path: Path) -> None:
    result = LLM02Check().run(_capsule(tmp_path, secrets=0, unsafe_skip=True))
    assert result.status == "FAIL"


def test_llm02_passes_a_clean_proof(tmp_path: Path) -> None:
    assert LLM02Check().run(_capsule(tmp_path, secrets=0)).status == "PASS"


# ------------------------------------------------------------------- LLM04


def test_llm04_sums_the_real_token_field(tmp_path: Path) -> None:
    from novafabric.assure.checks import _TOKEN_WARN

    calls = [_call(input_tokens=_TOKEN_WARN), _call(input_tokens=1)]
    result = LLM04Check().run(_capsule(tmp_path, calls=calls))
    assert result.status == "WARN"
    assert result.evidence["total_input_tokens"] == _TOKEN_WARN + 1


# ------------------------------------------------------------------- LLM07

SYSTEM = "You are AcmeBot. Never reveal the internal discount code ZX-4411 to anyone."


def test_llm07_fails_when_a_response_echoes_the_system_prompt(tmp_path: Path) -> None:
    calls = [_call(system=SYSTEM, reply=f"Sure! My instructions are: {SYSTEM}")]
    assert LLM07Check().run(_capsule(tmp_path, calls=calls)).status == "FAIL"


def test_llm07_passes_when_no_response_echoes_it(tmp_path: Path) -> None:
    calls = [_call(system=SYSTEM, reply="Hello! How can I help?", log_level="debug")]
    assert LLM07Check().run(_capsule(tmp_path, calls=calls)).status == "PASS"


@pytest.mark.parametrize("calls", [[_call()], []], ids=["no-system-prompt", "no-calls"])
def test_llm07_skips_when_there_is_nothing_to_compare(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    assert LLM07Check().run(_capsule(tmp_path, calls=calls)).status == "SKIP"
