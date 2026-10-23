"""``POST /capsule`` refuses a malformed event with ``422``, never a ``500``.

The accumulators index ``ev["event_type"]`` and write typed columns (``TEXT NOT NULL``,
``INTEGER``), so an event without ``event_type`` -- or with a non-string one, a
non-string ``run_id``/``model``, or a token count that is not an int32 -- raised
``KeyError``/``ArrowInvalid``/``ValueError`` inside the accumulator and the caller got
an opaque ``500``. The request model now types each event, so FastAPI rejects the
whole batch with ``422`` naming the field, before anything reaches the accumulator,
exactly as it already did for a malformed body.

These tests run against the real in-memory :class:`DuckDBAccumulator` (the default),
because a mock accumulator accepts anything and is how the defect went unnoticed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novafabric.collector_app.app import create_collector_app
from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator

INT32_MAX = 2**31 - 1


@pytest.fixture(autouse=True)
def _collector_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_COLLECTOR_TOKEN", raising=False)
    monkeypatch.delenv("NOVA_COLLECTOR_TENANT", raising=False)
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)


@pytest.fixture
def acc() -> Any:
    accumulator = DuckDBAccumulator(Path(":memory:"))
    yield accumulator
    accumulator.close()


def _client(acc: Any) -> TestClient:
    return TestClient(
        create_collector_app(accumulator=acc),
        client=("127.0.0.1", 50000),
        base_url="http://127.0.0.1:8090",
        raise_server_exceptions=False,
    )


def _rows(acc: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = acc.export_arrow("capsule_events").to_pylist()
    return rows


@pytest.mark.parametrize(
    ("event", "field"),
    [
        ({"type": "llm_call"}, "event_type"),
        ({"event_type": None}, "event_type"),
        ({"event_type": 5}, "event_type"),
        ({"event_type": ["model_call"]}, "event_type"),
        ({"event_type": ""}, "event_type"),
        ({"event_type": "model_call", "run_id": 7}, "run_id"),
        ({"event_type": "model_call", "run_id": None}, "run_id"),
        ({"event_type": "model_call", "model": 5}, "model"),
        ({"event_type": "model_call", "tool_name": {"a": 1}}, "tool_name"),
        ({"event_type": "model_call", "tokens_in": "abc"}, "tokens_in"),
        ({"event_type": "model_call", "tokens_in": None}, "tokens_in"),
        ({"event_type": "model_call", "tokens_out": INT32_MAX + 1}, "tokens_out"),
        ({"event_type": "model_call", "tokens_in": -1}, "tokens_in"),
        ({"event_type": "model_call", "tokens_in": 1.5}, "tokens_in"),
    ],
)
def test_a_malformed_event_is_refused_with_422_naming_the_field(
    acc: Any, event: dict[str, Any], field: str
) -> None:
    resp = _client(acc).post("/capsule", json={"capsule_id": "c1", "events": [event]})
    assert resp.status_code == 422, resp.text
    locs = [err["loc"] for err in resp.json()["detail"]]
    assert any(loc[:3] == ["body", "events", 0] and field in loc for loc in locs), locs
    assert _rows(acc) == []


def test_one_bad_event_refuses_the_whole_batch_and_does_not_poison_the_next(
    acc: Any,
) -> None:
    client = _client(acc)
    bad = client.post(
        "/capsule",
        json={"capsule_id": "c1", "events": [{"event_type": "model_call"}, {"x": 1}]},
    )
    assert bad.status_code == 422
    # the error points at the second event, the one without event_type
    assert any(err["loc"][:3] == ["body", "events", 1] for err in bad.json()["detail"])
    assert _rows(acc) == []  # nothing from the refused batch was ingested

    good = client.post(
        "/capsule", json={"capsule_id": "c2", "events": [{"event_type": "tool_call"}]}
    )
    assert good.status_code == 200
    assert good.json() == {"ok": True, "ingested": 1}
    rows = _rows(acc)
    assert [(r["run_id"], r["event_type"]) for r in rows] == [("c2", "tool_call")]


def test_a_well_formed_event_is_ingested_with_its_fields(acc: Any) -> None:
    event = {
        "event_type": "model_call",
        "run_id": "run-9",
        "model": "m-1",
        "tool_name": None,
        "tokens_in": INT32_MAX,
        "tokens_out": 0,
        "unknown_extra": {"kept": True},
    }
    resp = _client(acc).post("/capsule", json={"capsule_id": "c1", "events": [event]})
    assert resp.status_code == 200, resp.text
    [row] = _rows(acc)
    assert row["run_id"] == "run-9"
    assert row["event_type"] == "model_call"
    assert row["model"] == "m-1"
    assert row["tool_name"] == ""
    assert row["tokens_in"] == INT32_MAX
    assert row["tenant"] == "default"


def test_unknown_event_fields_still_reach_the_accumulator() -> None:
    """Events are an open shape: only the fields an accumulator writes are typed."""
    seen: list[list[dict[str, Any]]] = []

    class _Recorder:
        def ingest_capsule_events(self, events: list[dict[str, Any]]) -> int:
            seen.append(events)
            return len(events)

    resp = _client(_Recorder()).post(
        "/capsule",
        json={"capsule_id": "c1", "events": [{"event_type": "x", "span_id": "s1"}]},
    )
    assert resp.status_code == 200
    assert seen == [[{"event_type": "x", "span_id": "s1", "run_id": "c1", "tenant": "default"}]]


def test_clickhouse_fields_are_typed_too() -> None:
    """``model_id`` and ``tokens_total`` are what the ClickHouse accumulator writes."""

    class _ClickHouseShaped:
        def ingest_events(self, events: list[dict[str, Any]]) -> int:  # pragma: no cover
            raise AssertionError("must not be reached for a malformed event")

    client = _client(_ClickHouseShaped())
    for event in (
        {"event_type": "m", "model_id": 5},
        {"event_type": "m", "tokens_total": "lots"},
        {"event_type": "m", "tokens_total": 2**63},
    ):
        resp = client.post("/capsule", json={"capsule_id": "c1", "events": [event]})
        assert resp.status_code == 422, (event, resp.text)
