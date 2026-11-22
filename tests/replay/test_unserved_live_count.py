"""c09 (E7 ``model_calls_live_undercount``): a recorded model call replay cannot serve
is counted live when the replayed process actually sends it.

Unit level, on ``MockModelDispatcher._count_unserved_live`` -- the one place a request
no seam serves is counted. End to end (fake providers, ``--permissive``) in
``test_generic_wire_replay_e2e.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from novafabric.capture.hooks._url_registry import load_url_registry
from novafabric.replay._client_transport import handled_upstream
from novafabric.replay._contract import ReplayEventLog, read_events
from novafabric.replay._dispatcher import MockModelDispatcher
from novafabric.replay._servability import NOT_SERVABLE, classify

MISTRAL = "https://api.mistral.ai/v1/chat/completions"
OLLAMA = "http://localhost:11434"


def _request_only(system: str, endpoint: str) -> dict[str, Any]:
    """A logical model call recorded without its response (wire recording off)."""
    return {
        "model_call_id": f"m-{system}",
        "gen_ai.system": system,
        "gen_ai.response.choices": [],
        "status": "success",
        "endpoint": endpoint,
        "extensions": {"io.novafabric.record_role": "logical"},
    }


def _dispatcher(tmp_path: Path, records: list[dict[str, Any]]) -> tuple[Any, Path]:
    log = tmp_path / "events.jsonl"
    dispatcher = MockModelDispatcher(records, divergence_policy="warn",
                                     events=ReplayEventLog(log))
    dispatcher._registry = load_url_registry()
    return dispatcher, log


def _live(log: Path) -> list[dict[str, Any]]:
    return [e for e in read_events(log) if e.get("event") == "model_live"]


def test_a_request_to_an_unservable_system_is_counted_live_each_time(tmp_path: Path) -> None:
    records = [_request_only("mistral", MISTRAL)]
    assert classify(records) == [NOT_SERVABLE]
    dispatcher, log = _dispatcher(tmp_path, records)
    dispatcher._count_unserved_live(MISTRAL)
    dispatcher._count_unserved_live(MISTRAL + "?retry=1")  # a retry is a second request
    live = _live(log)
    assert len(live) == 2
    assert {e["system"] for e in live} == {"mistral"}
    assert {e["provider"] for e in live} == {"generic-wire"}


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1/chat/completions",  # not a registry model host
        f"{OLLAMA}/api/chat",  # a registry host, but the capsule recorded no ollama call
    ],
)
def test_a_request_to_another_system_is_not_counted(tmp_path: Path, url: str) -> None:
    dispatcher, log = _dispatcher(tmp_path, [_request_only("mistral", MISTRAL)])
    dispatcher._count_unserved_live(url)
    assert _live(log) == []


def test_a_metadata_request_runs_no_model_and_is_not_counted(tmp_path: Path) -> None:
    dispatcher, log = _dispatcher(tmp_path, [_request_only("ollama", f"{OLLAMA}/api/embed")])
    dispatcher._count_unserved_live(f"{OLLAMA}/api/tags")
    assert _live(log) == []
    dispatcher._count_unserved_live(f"{OLLAMA}/api/embed")  # runs a model: counted
    assert len(_live(log)) == 1


def test_a_request_already_counted_upstream_is_never_counted_again(tmp_path: Path) -> None:
    dispatcher, log = _dispatcher(tmp_path, [_request_only("mistral", MISTRAL)])
    with handled_upstream():
        dispatcher._count_unserved_live(MISTRAL)
    assert _live(log) == []


def test_a_request_a_dedicated_queue_claims_is_left_to_that_queue(tmp_path: Path) -> None:
    """An Ollama ``/api/chat`` request is claimed by the Ollama queue, which counts it
    live itself when it has nothing to serve: ``transport_target`` adds no count."""
    dispatcher, log = _dispatcher(tmp_path, [_request_only("ollama", f"{OLLAMA}/api/chat")])
    request = httpx.Request("POST", f"{OLLAMA}/api/chat", json={"model": "m"})
    assert dispatcher.transport_target(request) == "ollama.chat"
    assert _live(log) == []
    embed = httpx.Request("POST", f"{OLLAMA}/api/embed", json={"model": "m"})
    assert dispatcher.transport_target(embed) is None  # no queue: passed through, counted
    assert len(_live(log)) == 1


def test_a_capsule_with_only_servable_calls_counts_nothing(tmp_path: Path) -> None:
    dispatcher, log = _dispatcher(tmp_path, [])
    dispatcher._count_unserved_live(MISTRAL)
    assert _live(log) == []


def test_a_registry_that_cannot_classify_the_request_never_raises(tmp_path: Path) -> None:
    dispatcher, log = _dispatcher(tmp_path, [_request_only("mistral", MISTRAL)])

    class _Broken:
        def gen_ai_system(self, url: str) -> str:
            raise RuntimeError("boom")

    dispatcher._registry = _Broken()
    dispatcher._count_unserved_live(MISTRAL)
    assert _live(log) == []
