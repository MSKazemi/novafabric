"""ADR-0324 amendment: a model call is served whichever ``httpx`` transport the client uses.

Mocked replay serves generic-wire and Ollama calls at ``httpx.HTTPTransport`` /
``AsyncHTTPTransport``. Capture records at ``Client.send``, above the transport, so a
workload whose client sends through another transport -- ``httpx.MockTransport``, a
``BaseTransport`` subclass that delegates to ``HTTPTransport`` (what proxy and retry
libraries do), and their async variants -- was recorded with its response but never
reached the serving seam at replay: the recorded call was left unconsumed and
``model_calls_live`` read 0, so a ``--permissive`` replay reported success with a live
model call it never counted.

Each test captures a real agent process that posts chat completions to a registry model
host (the test registry's ``mistral`` entry) through one of those transports, then replays
it while the transport and the fake server would answer something else. Every request
that reaches the custom transport or the server is appended to ``AGENT_SIDE``.

* strict: the call is served from the capsule, or refused before it runs -- nothing
  reaches the transport;
* permissive: a call that runs live is counted once in ``model_calls_live`` and named
  (surface and ``gen_ai.system``) in the replay capsule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from _wire_agent import write_registry
from typer.testing import CliRunner

from novafabric.cli.main import app

pytest.importorskip("httpx")

runner = CliRunner()

AGENT = r'''
import asyncio, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

CANNED = os.environ["WIRE_CANNED"]
SIDE = os.environ["AGENT_SIDE"]


def side(line):
    with open(SIDE, "a") as fh:
        fh.write(line + "\n")


def answer(prompt):
    return {"id": "cmpl-1", "object": "chat.completion", "model": "mistral-small",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": f"{CANNED}: {prompt}"}}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        side("server")
        data = json.dumps(answer(body["messages"][0]["content"])).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_address[1]}/mistral/v1/chat/completions"


def mock_handler(request):
    side("transport")
    return httpx.Response(200, json=answer(json.loads(request.content)["messages"][0]["content"]))


async def async_mock_handler(request):
    return mock_handler(request)


class Delegating(httpx.BaseTransport):
    def __init__(self):
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request):
        side("transport")
        return self._inner.handle_request(request)


class AsyncDelegating(httpx.AsyncBaseTransport):
    def __init__(self):
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        side("transport")
        return await self._inner.handle_async_request(request)


KIND = os.environ["TRANSPORT"]
PROMPTS = json.loads(os.environ["PROMPTS"])


def body(prompt):
    return {"model": "mistral-small", "messages": [{"role": "user", "content": prompt}]}


def run_sync(transport):
    with httpx.Client(transport=transport) as client:
        return [client.post(URL, json=body(p)).json()["choices"][0]["message"]["content"]
                for p in PROMPTS]


async def run_async(transport):
    async with httpx.AsyncClient(transport=transport) as client:
        out = []
        for p in PROMPTS:
            r = await client.post(URL, json=body(p))
            out.append(r.json()["choices"][0]["message"]["content"])
        return out


if KIND == "mock":
    texts = run_sync(httpx.MockTransport(mock_handler))
elif KIND == "delegate":
    texts = run_sync(Delegating())
elif KIND == "mock-async":
    texts = asyncio.run(run_async(httpx.MockTransport(async_mock_handler)))
else:
    texts = asyncio.run(run_async(AsyncDelegating()))
with open(os.environ["AGENT_OUT"], "w") as fh:
    json.dump(texts, fh)
'''

KINDS = ["mock", "delegate", "mock-async", "delegate-async"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT, encoding="utf-8")
    paths = {
        "agent": agent,
        "out": tmp_path / "out.json",
        "side": tmp_path / "side.log",
        "runs": tmp_path / "runs",
        "replays": tmp_path / "replays",
    }
    monkeypatch.setenv("NOVAFABRIC_URL_REGISTRY", str(write_registry(tmp_path)))
    monkeypatch.setenv("AGENT_OUT", str(paths["out"]))
    monkeypatch.setenv("AGENT_SIDE", str(paths["side"]))
    for var in ("FORCE_COLOR", "COLORTERM", "OLLAMA_BASE_URL", "OLLAMA_HOST",
                "NOVAFABRIC_CAPTURE_WIRE_RESPONSES", "HTTP_PROXY", "HTTPS_PROXY",
                "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    return paths


def _side(env: dict[str, Path]) -> list[str]:
    return env["side"].read_text().splitlines() if env["side"].exists() else []


def _capture(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, kind: str, prompts: list[str]
) -> tuple[Path, list[str]]:
    monkeypatch.setenv("TRANSPORT", kind)
    monkeypatch.setenv("PROMPTS", json.dumps(prompts))
    monkeypatch.setenv("WIRE_CANNED", "capture-answer")
    result = runner.invoke(
        app, ["capture", "--output-dir", str(env["runs"]), sys.executable, str(env["agent"])]
    )
    assert result.exit_code == 0, result.output
    (capsule,) = [p for p in env["runs"].iterdir() if (p / "capsule.yaml").exists()]
    captured = json.loads(env["out"].read_text())
    assert "transport" in _side(env)  # the capture really went through the custom transport
    records = [json.loads(line) for line in
               (capsule / "model-calls.jsonl").read_text().splitlines() if line.strip()]
    assert len(records) == len(prompts)
    assert {r["extensions"]["io.novafabric.api_surface"] for r in records} == {"generic-wire"}
    env["out"].unlink()
    env["side"].unlink()
    return capsule, captured


def _replay(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsule: Path,
    prompts: list[str], *args: str,
) -> tuple[Any, dict[str, Any], Path]:
    monkeypatch.setenv("PROMPTS", json.dumps(prompts))
    monkeypatch.setenv("WIRE_CANNED", "REPLAY-LIVE-ANSWER")
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(env["replays"]), *args])
    (replay_dir,) = list(env["replays"].iterdir())
    doc = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    return result, doc, replay_dir


@pytest.mark.parametrize("kind", KINDS)
def test_a_call_through_a_custom_transport_is_served_from_the_capsule(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    capsule, captured = _capture(env, monkeypatch, kind, ["hi", "again"])
    assert captured == ["capture-answer: hi", "capture-answer: again"]

    result, doc, _ = _replay(env, monkeypatch, capsule, ["hi", "again"])
    assert result.exit_code == 0, (result.output, doc)
    assert json.loads(env["out"].read_text()) == captured
    assert _side(env) == []  # neither the custom transport nor the server saw a request
    assert doc["status"] == "success"
    assert doc["model_calls_mocked"] == doc["model_calls_available"] == 2
    contract = doc["replay_contract"]
    assert contract["model_calls_live"] == 0
    assert contract["model_calls_unconsumed"] == 0
    assert contract["divergences"] == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_changed_request_through_a_custom_transport_is_refused_before_it_runs(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    capsule, _ = _capture(env, monkeypatch, kind, ["hi", "again"])

    result, doc, _ = _replay(env, monkeypatch, capsule, ["hi", "CHANGED"])
    assert result.exit_code == 1, (result.output, doc)
    assert _side(env) == []  # refused at the client: the transport never ran
    assert doc["status"] != "success"
    contract = doc["replay_contract"]
    kinds = [d["kind"] for d in contract["divergences"]]
    assert "wire_request_mismatch" in kinds
    assert contract["model_calls_live"] == 0
    assert doc["model_calls_mocked"] == 1


@pytest.mark.parametrize("kind", KINDS)
def test_permissive_counts_a_live_call_through_a_custom_transport_once_and_names_it(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    capsule, _ = _capture(env, monkeypatch, kind, ["hi", "again"])

    result, doc, replay_dir = _replay(
        env, monkeypatch, capsule, ["hi", "CHANGED"], "--permissive"
    )
    assert result.exit_code == 0, (result.output, doc)
    assert json.loads(env["out"].read_text()) == [
        "capture-answer: hi", "REPLAY-LIVE-ANSWER: CHANGED"
    ]
    assert _side(env).count("transport") == 1  # it ran live, once
    contract = doc["replay_contract"]
    assert contract["model_calls_live"] == 1  # counted, and counted once
    assert doc["model_calls_mocked"] == 1
    assert "wire_request_mismatch" in [d["kind"] for d in contract["divergences"]]
    assert doc["fidelity"]["verdict"] != "faithful"
    replayed = [json.loads(line) for line in
                (replay_dir / "model-calls.jsonl").read_text().splitlines() if line.strip()]
    live = [r for r in replayed
            if r.get("extensions", {}).get("io.novafabric.replay_placeholder") == "live"]
    assert all("status" not in r for r in live)  # its outcome was not observed
    assert len(live) == 1
    assert live[0]["gen_ai.system"] == "mistral"
    assert live[0]["extensions"]["io.novafabric.replay_surface"] == (
        "generic wire (registry model hosts)"
    )
