"""ADR-0319 (issues #49, #52 part 1): mocked replay never reports success while a
recorded model call it cannot serve runs live, and says what it cannot observe.

Found 2026-11-19 (replay diagnosis, probe P7): a workload that POSTs an
Ollama-shaped ``/api/chat`` over ``httpx`` is captured as a model call
(``gen_ai.system: ollama``, ``gen_ai.response.choices: []``). Mocked replay
ignored the record: "model calls: 0 of 0 served", ``model_calls_live: 0``,
``status: success`` -- while the live server answered. Probe P5: an ``ssh``
child reached the network and the report said "0 live connections".

Each test here captures a real workload with ``nova capture`` (an in-process fake
Ollama server; the real ``openai`` SDK over an ``httpx.MockTransport``) and replays
it with ``nova replay``. At replay the fake servers answer something else, so the
workload's output shows who answered. No network beyond loopback.

Since ADR-0320 Ollama ``/api/chat`` is recorded with its response and served, so the
workload here calls ``/api/embed``; since ADR-0324 that call is recorded with its
response bytes too (generic wire) -- so these captures switch body recording off
(``NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off``), which leaves exactly the ADR-0319 case: a
model call recorded without a response.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay._servability import (
    NOT_SERVABLE,
    SERVABLE,
    TRANSPORT,
    classify,
    endpoint_host,
    not_servable_by_system,
)

pytest.importorskip("httpx")

REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = [
    json.loads((REPO / d / "replay-result.schema.json").read_text())
    for d in ("schemas", "src/novafabric/schemas")
]

runner = CliRunner()


def _flat(text: str) -> str:
    """CLI output with line wrapping undone: Rich wraps at the terminal width, and the
    width a phrase lands on depends on the port number printed before it."""
    return " ".join(text.split())

AGENT = r'''
import json, os, subprocess, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx

STEPS = os.environ["STEPS"].split(",")
ANSWER = os.environ.get("ANSWER", "capture-answer")
out = {}


class Ollama(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.dumps({"model": "probe-model", "created_at": "2026-01-01T00:00:00Z",
                           "message": {"role": "assistant", "content": ANSWER},
                           "done": True, "done_reason": "stop"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with open(os.environ["AGENT_SIDE"], "a") as f:
            f.write("ollama-server-hit\n")

    def log_message(self, *a):
        pass


if "openai" in STEPS:
    import openai

    def handler(request):
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-test",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": ANSWER}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    client = openai.OpenAI(api_key="sk-test-not-a-key", base_url="http://model.invalid/v1",
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    r = client.chat.completions.create(model="gpt-test", messages=[{"role": "user", "content": "hi"}])
    out["openai"] = r.choices[0].message.content
if "ollama" in STEPS:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_BASE_URL"] = f"http://127.0.0.1:{port}"
    # /api/embed runs a model but is not a surface replay serves (ADR-0320 serves only
    # /api/chat and /api/generate), so it stays an unservable recorded model call.
    r = httpx.post(f"http://127.0.0.1:{port}/api/embed", timeout=10,
                   json={"model": "probe-model", "stream": False,
                         "messages": [{"role": "user", "content": "hi"}]})
    out["ollama"] = r.json()["message"]["content"]
    srv.shutdown()
if "child" in STEPS:
    out["child_rc"] = subprocess.run(["/bin/true"]).returncode
with open(os.environ["AGENT_OUT"], "w") as f:
    json.dump(out, f)
'''


class Workload:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.out = root / "out.json"
        self.side = root / "side.log"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        for var in ("FORCE_COLOR", "COLORTERM", "OLLAMA_BASE_URL", "OLLAMA_HOST"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("COLUMNS", "400")
        # ADR-0324: keep the /api/embed record response-less (see the module docstring).
        monkeypatch.setenv("NOVAFABRIC_CAPTURE_WIRE_RESPONSES", "off")

    def capture(self, steps: str) -> Path:
        self._mp.setenv("STEPS", steps)
        self._mp.setenv("ANSWER", "capture-answer")
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        assert json.loads(self.out.read_text()).get("ollama", "capture-answer") == "capture-answer"
        self.out.unlink()
        if self.side.exists():
            self.side.unlink()
        return capsule

    def replay(self, capsule: Path, *flags: str) -> tuple[Any, dict[str, Any] | None]:
        self._mp.setenv("ANSWER", "REPLAY-LIVE-ANSWER")
        result = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        dirs = sorted(self.replays.iterdir())
        doc = yaml.safe_load((dirs[-1] / "replay_result.yaml").read_text())
        for schema in _SCHEMAS:
            jsonschema.validate(doc, schema)
        return result, doc

    @property
    def observed(self) -> dict[str, Any] | None:
        return json.loads(self.out.read_text()) if self.out.exists() else None

    @property
    def server_hits(self) -> int:
        return self.side.read_text().count("ollama-server-hit") if self.side.exists() else 0


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workload:
    pytest.importorskip("openai")
    return Workload(tmp_path, monkeypatch)


def _model_records(capsule: Path) -> list[dict[str, Any]]:
    text = (capsule / "model-calls.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ── P7: an Ollama call over httpx ────────────────────────────────────────────


def test_p7_the_capsule_records_the_ollama_call_without_a_response(work: Workload) -> None:
    capsule = work.capture("ollama")
    (record,) = _model_records(capsule)
    assert record["gen_ai.system"] == "ollama"
    assert record["gen_ai.response.choices"] == []
    assert classify([record]) == [NOT_SERVABLE]


def test_p7_strict_replay_refuses_before_spawning(work: Workload) -> None:
    capsule = work.capture("ollama")
    host = endpoint_host(_model_records(capsule)[0])
    result, doc = work.replay(capsule)
    assert result.exit_code == 1, result.output
    assert doc["status"] == "aborted"
    assert doc["error"]["type"] == "ModelSurfaceNotServable"
    assert doc["error"]["model_calls_not_servable_by_system"] == {
        "ollama": {"count": 1, "hosts": [host]}
    }
    message = doc["error"]["message"]
    assert "ollama: 1" in message and str(host) in message and "--permissive" in message
    assert doc["model_calls_not_servable"] == 1
    assert "exit_code" not in doc
    # Nothing ran: no workload output, no hit on the live server.
    assert work.observed is None
    assert work.server_hits == 0
    assert "ModelSurfaceNotServable" in result.output


def test_p7_permissive_runs_live_and_reports_it(work: Workload) -> None:
    capsule = work.capture("ollama")
    result, doc = work.replay(capsule, "--permissive")
    assert result.exit_code == 0, result.output
    assert work.observed == {"ollama": "REPLAY-LIVE-ANSWER"}  # the live server answered
    assert work.server_hits == 1
    assert doc["status"] == "success"
    assert doc["model_calls_not_servable"] == 1
    contract = doc["replay_contract"]
    (system,) = contract["model_calls_not_servable_by_system"]
    assert system == "ollama"
    assert contract["model_calls_not_servable_by_system"]["ollama"]["count"] == 1
    assert contract["divergences"][0]["kind"] == "model_surface_not_servable"
    assert "model_surface_not_servable" in doc["divergence_reason"]
    flat = _flat(result.output)  # the console wraps at the terminal width
    assert "model calls: 0 of 0" not in flat
    assert "model calls: 0 of 1 recorded served" in flat
    assert "1 NOT servable (ollama: 1" in flat


def test_p7_dry_run_lists_the_live_model_call(work: Workload) -> None:
    capsule = work.capture("ollama")
    host = endpoint_host(_model_records(capsule)[0])
    result, doc = work.replay(capsule, "--dry-run")
    assert result.exit_code == 1, result.output
    assert result.output.startswith("REFUSED: ")
    assert f"[LIVE] model call (ollama, {host})" in result.output
    assert doc["status"] == "dry_run"
    assert doc["error"]["type"] == "ModelSurfaceNotServable"
    assert doc["model_calls_not_servable"] == 1
    assert work.observed is None

    result, doc = work.replay(capsule, "--dry-run", "--permissive")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("WARNING (model_surface_not_servable, --permissive)")
    assert f"[LIVE] model call (ollama, {host})" in result.output
    assert "error" not in doc


# ── mixed: OpenAI served + Ollama unserved ───────────────────────────────────


def test_mixed_capsule_names_only_the_unserved_system(work: Workload) -> None:
    capsule = work.capture("openai,ollama")
    records = _model_records(capsule)
    assert sorted(classify(records)) == [NOT_SERVABLE, SERVABLE]

    result, doc = work.replay(capsule)
    assert result.exit_code == 1
    assert doc["error"]["model_calls_not_servable_by_system"].keys() == {"ollama"}
    assert doc["model_calls_not_servable"] == 1
    assert work.observed is None

    result, doc = work.replay(capsule, "--permissive")
    assert result.exit_code == 0, result.output
    # OpenAI was served from the capsule; Ollama answered live.
    assert work.observed == {"openai": "capture-answer", "ollama": "REPLAY-LIVE-ANSWER"}
    assert (doc["model_calls_mocked"], doc["model_calls_available"]) == (1, 1)
    assert doc["model_calls_not_servable"] == 1
    assert doc["replay_contract"]["model_calls_not_servable_by_system"].keys() == {"ollama"}
    assert "model calls: 1 of 2 recorded served from the capsule" in result.output


def test_a_served_capsule_with_transport_records_is_unchanged(work: Workload) -> None:
    """ADR-0305 transport records (the wire hook's HTTP attempts beneath an SDK
    call) are neither served nor refused. ``httpx.MockTransport`` bypasses the
    wire hook, so the attempts are added as the wire hook writes them: a retried
    attempt of the captured call, and an orphaned one whose SDK record is absent."""
    capsule = work.capture("openai")
    (sdk,) = _model_records(capsule)
    attempts = [
        {
            **sdk, "model_call_id": f"01HXAY7M5JZ8R7K4P9DPBYK2W{i}",
            "gen_ai.response.choices": [], "endpoint": "http://model.invalid/v1/chat/completions",
            "extensions": {"io.novafabric.record_role": "transport",
                           "io.novafabric.logical_call_id": logical},
        }
        for i, logical in enumerate([sdk["model_call_id"], "01HXAY7M5JZ8R7K4P9DPBYK2GONE"])
    ]
    records = [attempts[0], sdk, attempts[1]]
    (capsule / "model-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    assert classify(records) == [TRANSPORT, SERVABLE, TRANSPORT]
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.observed == {"openai": "capture-answer"}
    assert doc["status"] == "success"
    assert doc["model_calls_not_servable"] == 0
    contract = doc["replay_contract"]
    assert contract["model_calls_not_servable_by_system"] == {}
    assert contract["network_observation_scope"] == "python-process"
    assert contract["child_processes_observed"] is True
    assert contract["child_processes_spawned"] == 0
    assert "NOT servable" not in _flat(result.output)
    assert "child process" not in result.output


# ── #52 part 1: child processes ──────────────────────────────────────────────


def test_a_child_process_is_counted_and_named_with_a_warning(work: Workload) -> None:
    capsule = work.capture("openai,child")
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.observed == {"openai": "capture-answer", "child_rc": 0}
    contract = doc["replay_contract"]
    assert contract["network_observation_scope"] == "python-process"
    assert contract["child_processes_spawned"] == 1
    assert contract["child_processes_argv0"] == ["true"]
    # #52 part 2: not Python, so not observed -- and the result says so.
    assert contract["child_processes_unobserved"] == 1
    assert contract["network_observation_coverage"] == "partial"
    assert doc["status"] == "success"  # reported, never a failure
    assert "1 child process started during the replay (true)" in result.output
    assert "1 not observed" in _flat(result.output)


# ── the classification, record by record ─────────────────────────────────────


def _sdk(system: str = "openai", **extra: Any) -> dict[str, Any]:
    return {
        "model_call_id": f"01HXAY7M5JZ8R7K4P9DPBYK2{system[:2].upper()}",
        "gen_ai.system": system,
        "status": "success",
        "gen_ai.response.choices": [
            {"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}
        ],
        **extra,
    }


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_sdk("openai"), SERVABLE),
        (_sdk("anthropic"), SERVABLE),
        (_sdk("openai", **{"gen_ai.response.choices": []}), NOT_SERVABLE),
        (_sdk("ollama", endpoint="http://localhost:11434/api/chat"), NOT_SERVABLE),
        (_sdk("huggingface", endpoint="https://router.huggingface.co/v1/chat"), NOT_SERVABLE),
        (_sdk("openai", extensions={"io.novafabric.api_surface": "openai.unknown"}),
         NOT_SERVABLE),
        ("a line that is not an object", NOT_SERVABLE),
    ],
    ids=["openai", "anthropic", "no-response", "ollama", "hugging-face", "unknown-surface",
         "malformed"],
)
def test_classification(record: Any, expected: str) -> None:
    assert classify([record]) == [expected]


def test_a_recorded_sdk_error_holds_a_served_position() -> None:
    error = {
        **_sdk("openai", **{"gen_ai.response.choices": []}),
        "status": "error",
        "error": {"type": "RateLimitError", "message": "slow down"},
    }
    assert classify([error]) == [SERVABLE]


def test_transport_records_are_ignored_even_when_orphaned() -> None:
    transport = {
        **_sdk("openai", **{"gen_ai.response.choices": []}),
        "extensions": {
            "io.novafabric.record_role": "transport",
            "io.novafabric.logical_call_id": "01HXAY7M5JZ8R7K4P9DPBYK2ZZ",
        },
    }
    assert classify([transport]) == [TRANSPORT]
    assert not_servable_by_system([transport]) == {}


def test_hosts_never_carry_credentials_paths_or_queries() -> None:
    record = _sdk(
        "ollama", endpoint="http://user:s3cret@10.0.0.5:11434/api/chat?key=abc"
    )
    assert endpoint_host(record) == "10.0.0.5:11434"
    assert endpoint_host(_sdk("ollama", endpoint="http://[::1]:11434/x")) == "[::1]:11434"
    assert endpoint_host(_sdk("openai")) is None
    by_system = not_servable_by_system([record, record, _sdk("huggingface")])
    assert by_system == {
        "ollama": {"count": 2, "hosts": ["10.0.0.5:11434"]},
        "huggingface": {"count": 1, "hosts": []},
    }
