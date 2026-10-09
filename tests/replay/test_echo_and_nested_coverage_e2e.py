"""ADR-0306 slice 4, end to end: the D10 echo check and nested-boundary coverage.

A real agent runs under ``nova capture`` (the real ``openai`` SDK over an
``httpx.MockTransport`` serving canned responses) and is then re-run by mocked
``nova replay`` with the network refused. It:

1. asks the model, which requests ``get_weather``;
2. runs ``get_weather`` -- an **undeclared** function, so its result reaches the
   model only as the ``role: "tool"`` message it sends back (fact 6);
3. asks the model again with that tool message;
4. calls ``research`` -- a ``record.tool`` boundary whose body itself calls the
   model (a *nested* model call);
5. asks the model a final question.

D10 (report-only, owner Q10): the tool message the replay sends back is compared
with the recorded one; a difference is reported under
``replay_contract.tool_result_echo`` and never fails the replay. Nested
coverage: ``research`` is served without running its body, and the model call it
made at capture is consumed as *covered* -- so step 5 still receives its own
recorded answer and the queues are fully consumed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from _mocked_replay_agent import openai_body
from typer.testing import CliRunner

from novafabric.cli.main import app

pytest.importorskip("openai")

runner = CliRunner()

_RESULT_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "src" / "novafabric" / "schemas"
     / "replay-result.schema.json").read_text()
)

#: A detected secret (ADR-0009 ``github-token``), split so no scanner flags this file.
GITHUB_TOKEN = "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"

AGENT = r'''
import json, os
import httpx
from openai import OpenAI
from novafabric.capture import record

CANNED = json.loads(os.environ.get("AGENT_CANNED") or "null")
_served = [0]


def _transport(request):
    if CANNED is None:
        raise RuntimeError("network reached during replay")
    body = CANNED[_served[0]]
    _served[0] += 1
    return httpx.Response(200, json=body)


client = OpenAI(api_key="sk-test", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(_transport)))


def side(text):
    with open(os.environ["AGENT_SIDE"], "a") as fh:
        fh.write(text + "\n")


def get_weather(city):
    # Undeclared: nothing intercepts it; it runs live in replay too.
    return f"{city}: {os.environ['WEATHER']}"


@record.tool(mutation_class="none")
def research(q):
    side(f"research {q}")
    r = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": q}])
    return {"summary": r.choices[0].message.content}


def ask(messages):
    return client.chat.completions.create(model="gpt-4o", messages=messages)


out = {}
messages = [{"role": "user", "content": "weather in Paris?"}]
first = ask(messages)
call = first.choices[0].message.tool_calls[0]
messages.append({"role": "assistant", "content": None, "tool_calls": [{
    "id": call.id, "type": "function",
    "function": {"name": call.function.name, "arguments": call.function.arguments}}]})
if os.environ.get("SEND_TOOL_RESULT", "1") == "1":
    messages.append({"role": "tool", "tool_call_id": call.id,
                     "content": get_weather(json.loads(call.function.arguments)["city"])})
out["second"] = ask(messages).choices[0].message.content
if os.environ.get("CALL_RESEARCH", "1") == "1":
    out["research"] = research("topic")
out["final"] = ask([{"role": "user", "content": "after"}]).choices[0].message.content
with open(os.environ["AGENT_OUT"], "w") as fh:
    json.dump(out, fh)
'''

CANNED = [
    openai_body(tool_calls=[("call_W", "get_weather", {"city": "Paris"})], rid="r1"),
    openai_body(content="It is sunny.", rid="r2"),
    openai_body(content="nested-answer", rid="r3"),
    openai_body(content="final-answer", rid="r4"),
]


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.out = root / "out.json"
        self.side = root / "side.log"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        monkeypatch.setenv("WEATHER", "sunny")
        for var in ("FORCE_COLOR", "COLORTERM", "SEND_TOOL_RESULT", "CALL_RESEARCH"):
            monkeypatch.delenv(var, raising=False)

    def capture(self) -> tuple[Path, dict[str, Any]]:
        self._mp.setenv("AGENT_CANNED", json.dumps(CANNED))
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        self._mp.delenv("AGENT_CANNED")
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        captured = json.loads(self.out.read_text())
        self.out.unlink()
        self.side.unlink()
        return capsule, captured

    def replay(self, capsule: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
        before = set(self.replays.iterdir()) if self.replays.exists() else set()
        result = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays)])
        (replay_dir,) = set(self.replays.iterdir()) - before
        replay = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
        jsonschema.validate(replay, _RESULT_SCHEMA)
        replay["_cli_output"] = " ".join(result.output.split())  # unwrap the terminal
        observed = json.loads(self.out.read_text()) if self.out.exists() else {}
        if self.out.exists():
            self.out.unlink()
        return replay, observed, replay_dir

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []

    @staticmethod
    def records(path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def _echo(replay: dict[str, Any]) -> dict[str, Any]:
    echo = replay["replay_contract"]["tool_result_echo"]
    assert echo["policy"] == "report-only"
    return echo


# ── D10: the echo check ──────────────────────────────────────────────────────


def test_an_unchanged_tool_result_echo_is_reported_matched(env: Env) -> None:
    capsule, captured = env.capture()
    replay, replayed, _ = env.replay(capsule)
    assert replayed == captured
    assert replay["status"] == "success", replay.get("divergence_reason")
    echo = _echo(replay)
    assert (echo["matched"], echo["mismatched"], echo["not_checked"]) == (1, 0, 0)
    assert "tool-result echoes" in replay["_cli_output"]


def test_a_changed_tool_result_is_reported_but_never_fails_the_replay(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _ = env.capture()
    monkeypatch.setenv("WEATHER", "hail-SENTINEL-41c7")
    replay, _, replay_dir = env.replay(capsule)
    # Report-only (owner Q10): the replay still succeeds and lists no divergence.
    assert replay["status"] == "success"
    assert replay["replay_contract"]["divergences"] == []
    echo = _echo(replay)
    assert (echo["matched"], echo["mismatched"]) == (0, 1)
    (mismatch,) = echo["mismatches"]
    assert mismatch["kind"] == "tool_result_echo_mismatch"
    assert mismatch["tool_call_id"] == "call_W"
    assert mismatch["call_index"] == 1
    assert "differs from the recorded one" in mismatch["message"]
    assert "1 mismatched (report-only)" in replay["_cli_output"]
    # Never a value: neither the live nor the recorded tool result is written.
    for path in replay_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert "SENTINEL-41c7" not in text and "sunny" not in text, path


def test_a_tool_result_the_replay_no_longer_sends_is_reported(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _ = env.capture()
    monkeypatch.setenv("SEND_TOOL_RESULT", "0")
    replay, _, _ = env.replay(capsule)
    assert replay["status"] == "success"
    (mismatch,) = _echo(replay)["mismatches"]
    assert "sent no result" in mismatch["reason"]


def test_a_redacted_secret_in_a_tool_result_still_echoes_as_matched(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEATHER", GITHUB_TOKEN)
    capsule, _ = env.capture()
    # The capsule scanner masked the secret inside the recorded tool message ...
    text = (capsule / "model-calls.jsonl").read_text()
    assert GITHUB_TOKEN not in text and "[REDACTED:" in text
    # ... and the live function sends the raw value back: redact-then-digest on
    # both sides makes them equal, so no false mismatch is reported.
    replay, _, replay_dir = env.replay(capsule)
    echo = _echo(replay)
    assert (echo["matched"], echo["mismatched"]) == (1, 0)
    for path in replay_dir.rglob("*"):
        if path.is_file():
            assert GITHUB_TOKEN not in path.read_text(errors="replace"), path


def test_a_capsule_without_request_messages_reports_not_checked_never_matched(
    env: Env,
) -> None:
    capsule, _ = env.capture()
    path = capsule / "model-calls.jsonl"
    records = env.records(path)
    for record in records:
        record.pop("gen_ai.request.messages", None)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    replay, _, _ = env.replay(capsule)
    echo = _echo(replay)
    assert (echo["matched"], echo["mismatched"], echo["not_checked"]) == (0, 0, 1)
    assert echo["not_checked_reasons"] == {
        "the capsule recorded no request messages for this model call": 1
    }


# ── nested-boundary coverage ─────────────────────────────────────────────────


def _research_record(capsule: Path) -> dict[str, Any]:
    (rec,) = [r for r in Env.records(capsule / "tool-calls.jsonl")
              if r["tool_name"] == "research"]
    return rec


def test_a_nested_boundary_is_served_and_its_nested_model_call_is_covered(env: Env) -> None:
    capsule, captured = env.capture()
    rec = _research_record(capsule)
    assert rec["extensions"]["io.novafabric.result_codec"] == "json-v1"
    assert rec["extensions"]["io.novafabric.nested_records"] >= 1
    nested = [r for r in Env.records(capsule / "model-calls.jsonl")
              if (r.get("extensions") or {}).get("io.novafabric.within_tool_call_id")
              == rec["tool_call_id"]]
    assert nested, "the model call inside research() was not marked nested"

    replay, replayed, _ = env.replay(capsule)
    assert env.side_effects == [], "research() ran its body during mocked replay"
    assert replayed == captured
    assert replayed["research"] == {"summary": "nested-answer"}
    # The call after the boundary received its OWN recorded answer: the nested
    # record was skipped, not served to it.
    assert replayed["final"] == "final-answer"
    assert replay["status"] == "success", replay.get("divergence_reason")
    contract = replay["replay_contract"]
    assert contract["model_calls_covered"] == 1
    assert replay["model_calls_mocked"] == 3
    assert replay["tool_calls_mocked"] == 1
    assert replay["queues_fully_consumed"] is True


def test_a_slice_1_capsule_with_a_nested_boundary_is_served_without_recapture(
    env: Env,
) -> None:
    from novafabric.capture._tool_codec import legacy_nested_reason

    capsule, captured = env.capture()
    path = capsule / "tool-calls.jsonl"
    records = env.records(path)
    for rec in records:
        ext = rec.get("extensions") or {}
        if rec["tool_name"] == "research":  # rewrite to exactly what slice 1 wrote
            ext["io.novafabric.result_codec"] = "not-servable"
            ext["io.novafabric.not_servable_reason"] = legacy_nested_reason(
                ext["io.novafabric.nested_records"]
            )
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    replay, replayed, _ = env.replay(capsule)
    assert env.side_effects == []
    assert replayed == captured
    assert replay["status"] == "success", replay.get("divergence_reason")


def test_an_unmarked_nested_call_fails_closed(env: Env) -> None:
    """A nested call capture could not mark (a raw thread, ADR-0224 D3) is never
    covered: the served boundary leaves it in the queue and strict replay fails."""
    capsule, _ = env.capture()
    path = capsule / "model-calls.jsonl"
    records = env.records(path)
    for record in records:
        (record.get("extensions") or {}).pop("io.novafabric.within_tool_call_id", None)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    replay, replayed, _ = env.replay(capsule)
    assert env.side_effects == []
    assert replay["replay_contract"]["model_calls_covered"] == 0
    assert replayed["final"] == "nested-answer"  # the wrong answer is visible ...
    assert replay["status"] == "failure"  # ... and the replay fails closed
    assert any(
        d["kind"] == "model_calls_unconsumed" for d in replay["replay_contract"]["divergences"]
    )
