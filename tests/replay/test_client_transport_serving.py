"""ADR-0324 amendment, unit level: serving whichever ``httpx`` transport a client uses.

``MockModelDispatcher`` patches ``Client._transport_for_url`` (``httpx``, ``httpx2``):
a transport that is not a serving seam itself is wrapped, so its Ollama and generic-wire
calls are served, refused or counted live; a request already handled upstream -- by the
wrapper, or by an SDK method replay refused -- is never claimed a second time.

No network: a request that is served never reaches the workload's transport, and the
``MockTransport`` handlers below record every request that does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from novafabric.capture.hooks._wire_body import WIRE_REQUEST_EXT, WIRE_RESPONSE_EXT, request_key
from novafabric.replay._client_transport import (
    AsyncServingTransport,
    ServingTransport,
    handled_upstream,
)
from novafabric.replay._contract import ReplayEventLog, read_events, summarize
from novafabric.replay._dispatcher import MockModelDispatcher
from novafabric.replay._errors import ReplayDivergenceError

OLLAMA = "http://192.0.2.1:11434"
MISTRAL = "https://api.mistral.ai/v1/chat/completions"
BODY = {"model": "mistral-small", "messages": [{"role": "user", "content": "hi"}]}


def _ollama_record(text: str) -> dict[str, Any]:
    return {
        "model_call_id": f"id-{text}",
        "gen_ai.system": "ollama",
        "gen_ai.request.model": "m",
        "gen_ai.response.model": "m",
        "gen_ai.response.choices": [{"index": 0, "message": {
            "role": "assistant", "content": text}, "finish_reason": "stop"}],
        "status": "success",
        "endpoint": f"{OLLAMA}/api/chat",
        "extensions": {
            "io.novafabric.api_surface": "ollama.chat",
            "io.novafabric.record_role": "logical",
            "io.novafabric.ollama.final": {"done_reason": "stop"},
        },
    }


def _wire_record(text: str) -> dict[str, Any]:
    body = json.dumps({"choices": [{"message": {"content": text}}]})
    return {
        "model_call_id": f"wire-{text}",
        "gen_ai.system": "mistral",
        "gen_ai.response.choices": [],
        "status": "success",
        "endpoint": MISTRAL,
        "extensions": {
            "io.novafabric.record_role": "logical",
            "io.novafabric.api_surface": "generic-wire",
            WIRE_REQUEST_EXT: request_key("POST", MISTRAL, json.dumps(BODY).encode()),
            WIRE_RESPONSE_EXT: {
                "v": 1, "status": 200, "reason": "OK",
                "headers": [["content-type", "application/json"]],
                "streamed": False, "complete": True, "bytes": len(body), "chunks": [body],
            },
        },
    }


class _Seen:
    """A ``MockTransport`` handler that answers ``LIVE`` and remembers each request."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "LIVE"}}],
                                         "message": {"role": "assistant", "content": "LIVE"},
                                         "done": True})


@contextmanager
def _install(
    records: list[dict[str, Any]], events: Path, policy: str = "fail"
) -> Iterator[MockModelDispatcher]:
    dispatcher = MockModelDispatcher(
        records, divergence_policy=policy, events=ReplayEventLog(events)
    )
    dispatcher.install()
    try:
        yield dispatcher
    finally:
        dispatcher.uninstall()


def _kinds(events: Path) -> list[str]:
    return [str(e.get("event")) for e in read_events(events)]


@pytest.fixture
def events(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


def test_an_ollama_call_through_a_mock_transport_is_served(events: Path) -> None:
    seen = _Seen()
    with _install([_ollama_record("one"), _ollama_record("two")], events):
        with httpx.Client(transport=httpx.MockTransport(seen)) as client:
            first = client.post(f"{OLLAMA}/api/chat", json={"model": "m", "stream": False})

        async def _async() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.MockTransport(seen)) as client:
                return await client.post(f"{OLLAMA}/api/chat", json={"model": "m", "stream": False})

        second = asyncio.run(_async())
    assert seen.urls == []
    assert first.json()["message"]["content"] == "one"
    assert second.json()["message"]["content"] == "two"
    report = summarize([_ollama_record("one"), _ollama_record("two")], [], read_events(events),
                       divergence_policy="fail", substitute_tools=True)
    assert report.model_calls_mocked == 2 and report.model_calls_live == 0


def test_an_unclaimed_request_reaches_the_workloads_transport_unchanged(events: Path) -> None:
    seen = _Seen()
    with _install([_wire_record("one")], events):
        with httpx.Client(transport=httpx.MockTransport(seen)) as client:
            response = client.get("https://example.org/not-a-model?q=1")
    assert seen.urls == ["https://example.org/not-a-model?q=1"]
    assert response.status_code == 200
    assert "model_live" not in _kinds(events) and "model_served" not in _kinds(events)


def test_only_a_transport_that_is_not_a_serving_seam_is_wrapped(events: Path) -> None:
    class Inherits(httpx.HTTPTransport):
        pass

    class Overrides(httpx.HTTPTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return super().handle_request(request)

    url = httpx.URL(MISTRAL)
    own = vars(httpx.Client)["_transport_for_url"]
    with _install([_wire_record("one")], events):
        plain = httpx.Client()
        assert plain._transport_for_url(url) is plain._transport
        inherits = httpx.Client(transport=Inherits())
        assert inherits._transport_for_url(url) is inherits._transport
        for transport in (Overrides(), httpx.MockTransport(_Seen())):
            client = httpx.Client(transport=transport)
            assert isinstance(client._transport_for_url(url), ServingTransport)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(_Seen()))
        assert isinstance(async_client._transport_for_url(url), AsyncServingTransport)
    # Uninstalled: the method is httpx's own again.
    after = httpx.Client(transport=httpx.MockTransport(_Seen()))
    assert after._transport_for_url(url) is after._transport
    assert vars(httpx.Client)["_transport_for_url"] is own


def test_strict_refuses_a_changed_request_before_the_transport_runs(events: Path) -> None:
    seen = _Seen()
    with _install([_wire_record("one")], events):
        with httpx.Client(transport=httpx.MockTransport(seen)) as client:
            with pytest.raises(ReplayDivergenceError, match="differs from the recorded request"):
                client.post(MISTRAL, json={**BODY, "temperature": 1})
    assert seen.urls == []


def test_a_request_handled_upstream_is_never_claimed_again(events: Path) -> None:
    """A delegating transport's inner ``HTTPTransport`` and a nested client see a request
    the wrapper already passed on: it is not served or counted a second time."""
    seen = _Seen()
    with _install([_wire_record("one")], events, policy="warn"):
        with handled_upstream(), httpx.Client(transport=httpx.MockTransport(seen)) as client:
            client.post(MISTRAL, json=BODY)
    assert seen.urls == [MISTRAL]
    assert _kinds(events).count("model_live") == 0
    assert "model_served" not in _kinds(events)


def test_a_refused_sdk_call_run_live_is_counted_once_and_never_served_on_the_wire(
    events: Path,
) -> None:
    """``openai.completions.create`` (legacy) is not served: ``--permissive`` runs it and
    counts it at the SDK method. Its HTTP request to ``api.openai.com`` -- a registry
    model host -- must not then be claimed by the generic wire engine too: that would
    count the call twice and consume another call's recorded answer."""
    openai = pytest.importorskip("openai")
    httpx2 = pytest.importorskip("httpx2")
    seen: list[str] = []

    def handler(request: Any) -> Any:
        seen.append(str(request.url))
        return httpx2.Response(200, json={
            "id": "c", "object": "text_completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "text": "LIVE", "finish_reason": "stop"}],
        })

    records = [_wire_record("one")]
    with _install(records, events, policy="warn"):
        client = openai.OpenAI(
            api_key="sk-test", max_retries=0,
            http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        )
        completion = client.completions.create(model="m", prompt="hi")
    assert completion.choices[0].text == "LIVE"
    assert len(seen) == 1
    report = summarize(records, [], read_events(events), divergence_policy="warn",
                       substitute_tools=True)
    assert report.model_calls_live == 1  # once, at the SDK method
    assert report.model_calls_unconsumed == 1  # the wire record was not taken by it
    kinds = [d["kind"] for d in report.divergences]
    assert "wire_request_mismatch" not in kinds


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_the_client_library_picks_each_requests_transport_through_the_patched_method(
    module_name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conformance with the real library: the client seam relies on the private
    ``Client._transport_for_url`` being how ``send`` picks a request's transport (sync
    and async). If a release stops calling it, this fails instead of replay silently
    serving ``HTTPTransport`` clients only."""
    module = pytest.importorskip(module_name)
    picked: list[str] = []

    def answer(request: Any) -> Any:
        return module.Response(200, text="picked")

    def sync_pick(client: Any, url: Any) -> Any:
        picked.append("sync")
        return module.MockTransport(answer)

    def async_pick(client: Any, url: Any) -> Any:
        picked.append("async")
        return module.MockTransport(answer)

    monkeypatch.setattr(module.Client, "_transport_for_url", sync_pick)
    monkeypatch.setattr(module.AsyncClient, "_transport_for_url", async_pick)
    with module.Client() as client:
        assert client.get("http://192.0.2.1/x").text == "picked"

    async def _async() -> str:
        async with module.AsyncClient() as client:
            return str((await client.get("http://192.0.2.1/x")).text)

    assert asyncio.run(_async()) == "picked"
    assert picked == ["sync", "async"]


def test_the_report_names_the_client_seam(events: Path) -> None:
    with _install([_wire_record("one")], events) as dispatcher:
        assert "httpx client transports (any transport)" in dispatcher.installed_surfaces
