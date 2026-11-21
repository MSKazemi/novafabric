"""W0 (replay strategy 2026-11-21): Google Gemini / Vertex calls are recorded and refused.

Before: neither host was in the URL registry, so a Gemini or Vertex call over ``httpx``
(the ``google-genai`` SDK's REST transport) left no model-call record. A strict mocked
replay then neither served nor refused it: it ran live and showed up only as a
connection count -- the original Ollama defect again.

Now the registry classifies ``generativelanguage.googleapis.com`` (``gcp.gemini``) and
``aiplatform.googleapis.com`` / ``<region>-aiplatform.googleapis.com``
(``gcp.vertex_ai``), so capture records the call. By default the generic wire layer
(ADR-0324) also records the response and replay serves it at the ``httpx`` transport
(served end to end in ``test_generic_wire_replay_e2e.py``). With response recording off
(``NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off``) the record is request only, like Mistral or
Cohere before ADR-0324, and a strict replay refuses up front with
``ModelSurfaceNotServable``; ``--permissive`` runs it and counts it as not servable.

The workload posts over ``httpx.MockTransport``: nothing reaches the network.
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

from novafabric.capture.event_recorder import _is_ai_api
from novafabric.capture.hooks._url_registry import load_url_registry
from novafabric.cli.main import app
from novafabric.replay._servability import NOT_SERVABLE, classify

pytest.importorskip("httpx")

REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = [
    json.loads((REPO / d / "replay-result.schema.json").read_text())
    for d in ("schemas", "src/novafabric/schemas")
]
runner = CliRunner()

GEMINI = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
)
VERTEX = (
    "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/"
    "publishers/google/models/gemini-2.0-flash:generateContent"
)
VERTEX_GLOBAL = (
    "https://aiplatform.googleapis.com/v1/projects/p/locations/global/"
    "publishers/google/models/gemini-2.0-flash:generateContent"
)
FAKE_KEY = "AIzaSyFAKE-not-a-key-0000000000000000000"

AGENT = r'''
import json, os
import httpx

ANSWER = os.environ.get("ANSWER", "capture-answer")


def handler(request):
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [{"text": ANSWER}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
    })


client = httpx.Client(transport=httpx.MockTransport(handler))
r = client.post(os.environ["GEMINI_URL"], headers={"x-goog-api-key": os.environ["KEY"]},
                json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
with open(os.environ["AGENT_OUT"], "w") as f:
    json.dump({"text": r.json()["candidates"][0]["content"]["parts"][0]["text"]}, f)
'''


# ── the registry ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "system"),
    [
        (GEMINI, "gcp.gemini"),
        ("https://generativelanguage.googleapis.com:443/v1/models/x:streamGenerateContent",
         "gcp.gemini"),
        (VERTEX, "gcp.vertex_ai"),
        (VERTEX_GLOBAL, "gcp.vertex_ai"),
        ("https://europe-west4-aiplatform.googleapis.com/v1beta1/x:predict", "gcp.vertex_ai"),
        ("https://US-AIPLATFORM.GOOGLEAPIS.COM/v1/x", "gcp.vertex_ai"),
    ],
)
def test_gemini_and_vertex_hosts_are_model_hosts(url: str, system: str) -> None:
    registry = load_url_registry()
    assert registry.gen_ai_system(url) == system
    assert _is_ai_api(url)  # the network-event flag agrees with the registry


@pytest.mark.parametrize(
    "url",
    [
        "https://generativelanguage.googleapis.com.attacker.example/v1/x",
        "https://evil.example/?u=https://generativelanguage.googleapis.com/",
        "https://evil.example/generativelanguage.googleapis.com/v1",
        "https://xaiplatform.googleapis.com/v1/x",
        "https://us-central1-aiplatform.googleapis.com.attacker.example/v1/x",
        "https://user@evil.example/aiplatform.googleapis.com/",
        "http://aiplatform.googleapis.com/v1/x",  # never plain HTTP
        "https://storage.googleapis.com/v1/b",
        "https://oauth2.googleapis.com/token",
    ],
)
def test_look_alikes_and_other_google_apis_are_not_gemini_or_vertex(url: str) -> None:
    assert not load_url_registry().gen_ai_system(url).startswith("gcp.")


# ── end to end: nova capture, then nova replay ───────────────────────────────


class Workload:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.out = root / "out.json"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("KEY", FAKE_KEY)
        monkeypatch.setenv("COLUMNS", "400")
        for var in ("FORCE_COLOR", "COLORTERM"):
            monkeypatch.delenv(var, raising=False)

    def capture(self, url: str, *, wire_responses: bool = True) -> Path:
        self._mp.setenv("GEMINI_URL", url)
        if wire_responses:
            self._mp.delenv("NOVAFABRIC_CAPTURE_WIRE_RESPONSES", raising=False)
        else:
            self._mp.setenv("NOVAFABRIC_CAPTURE_WIRE_RESPONSES", "off")
        self._mp.setenv("ANSWER", "capture-answer")
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        assert json.loads(self.out.read_text()) == {"text": "capture-answer"}
        self.out.unlink()
        return capsule

    def replay(self, capsule: Path, *flags: str) -> tuple[Any, dict[str, Any]]:
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


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workload:
    return Workload(tmp_path, monkeypatch)


def _records(capsule: Path) -> list[dict[str, Any]]:
    text = (capsule / "model-calls.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("url", "system", "host"),
    [
        (GEMINI, "gcp.gemini", "generativelanguage.googleapis.com"),
        (VERTEX, "gcp.vertex_ai", "us-central1-aiplatform.googleapis.com"),
    ],
)
def test_without_wire_responses_strict_replay_refuses_it_up_front(
    work: Workload, url: str, system: str, host: str
) -> None:
    capsule = work.capture(url, wire_responses=False)
    (record,) = _records(capsule)
    assert record["gen_ai.system"] == system
    assert classify([record]) == [NOT_SERVABLE]
    for path in capsule.rglob("*"):  # the API key reached no capsule file
        if path.is_file():
            assert FAKE_KEY not in path.read_text(errors="replace"), path

    result, doc = work.replay(capsule)
    assert result.exit_code == 1, result.output
    assert doc["status"] == "aborted"
    assert doc["error"]["type"] == "ModelSurfaceNotServable"
    assert doc["error"]["model_calls_not_servable_by_system"] == {
        system: {"count": 1, "hosts": [host]}
    }
    assert work.observed is None  # refused before the workload ran


def test_without_wire_responses_permissive_runs_it_and_counts_it_not_servable(
    work: Workload,
) -> None:
    capsule = work.capture(GEMINI, wire_responses=False)
    result, doc = work.replay(capsule, "--permissive")
    assert result.exit_code == 0, result.output
    assert work.observed == {"text": "REPLAY-LIVE-ANSWER"}  # not served: it ran
    assert doc["status"] == "success"
    assert doc["model_calls_not_servable"] == 1
    contract = doc["replay_contract"]
    assert contract["model_calls_not_servable_by_system"]["gcp.gemini"]["count"] == 1
    assert contract["divergences"][0]["kind"] == "model_surface_not_servable"
    assert doc["fidelity"]["verdict"] != "faithful"


@pytest.mark.parametrize(("url", "system"), [(GEMINI, "gcp.gemini"), (VERTEX, "gcp.vertex_ai")])
def test_by_default_the_response_is_recorded_and_the_record_is_servable(
    work: Workload, url: str, system: str
) -> None:
    capsule = work.capture(url)
    (record,) = _records(capsule)
    assert record["gen_ai.system"] == system
    assert record["extensions"]["io.novafabric.api_surface"] == "generic-wire"
    assert classify([record]) != [NOT_SERVABLE]
    for path in capsule.rglob("*"):  # the API key reached no capsule file
        if path.is_file():
            assert FAKE_KEY not in path.read_text(errors="replace"), path


def test_a_call_through_a_custom_transport_is_served_from_the_capsule(
    work: Workload,
) -> None:
    """The workload sends through its own ``httpx.MockTransport``, not
    ``httpx.HTTPTransport``. Replay wraps the transport the client picks
    (``Client._transport_for_url``, ADR-0324 amendment), so the call is served from the
    capsule and the workload's transport never runs. Before, the call bypassed serving:
    it ran on the mock transport, the record stayed unconsumed and ``model_calls_live``
    read 0."""
    capsule = work.capture(GEMINI)
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.observed == {"text": "capture-answer"}  # served, not the live answer
    assert doc["status"] == "success"
    contract = doc["replay_contract"]
    assert contract["divergences"] == []
    assert contract["model_calls_live"] == 0
    assert doc["model_calls_mocked"] == 1
