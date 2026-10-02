"""ADR-0234 D2 on the ClickHouse branch of ``/api/cost/report``.

The SQLite/capsule path reads each call's ``priced`` flag, so a model with no
catalog price cannot read as "$0.00 / free". The ClickHouse branch selected only
``sum(cost_usd)`` and stamped ``pricing_coverage_checked: false`` -- the same
failure the rule exists to prevent, on the backend used at cluster scale.

ClickHouse is not available in CI, so the client is a fake that answers by
inspecting the SQL, like the sibling cost-store tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from novafabric.cost import clickhouse_store

TOKEN = "cost-token-0123456789abcdef"
HEADERS = {"host": "127.0.0.1:4321"}


def _client(*, calls: int, unpriced_calls: int, unpriced: list[tuple[str, int]]) -> MagicMock:
    """A fake ClickHouse client whose rows depend on the SQL asked."""
    client = MagicMock()

    def query(sql: str, parameters: dict[str, Any]) -> MagicMock:
        res = MagicMock()
        if "AS total_input" in sql:
            res.result_rows = [[10, 5, 0, 0.0, calls, unpriced_calls]]
        elif "priced = 0" in sql:
            res.result_rows = [[m, n] for m, n in unpriced]
        else:
            res.result_rows = [["mystery-model", "x", 10, 5, 0, 0.0, calls]]
        return res

    client.query.side_effect = query
    return client


def test_all_unpriced_window_is_not_reported_as_zero_cost() -> None:
    client = _client(calls=3, unpriced_calls=3, unpriced=[("mystery-model", 3)])
    with patch.object(clickhouse_store, "_get_client", return_value=client):
        out = clickhouse_store.cost_report(days=7)
    assert out["pricing_coverage_checked"] is True
    assert out["totals"]["cost_usd"] is None, "0.0 here would read as free"
    assert out["unpriced_calls"] == 3 and out["priced_calls"] == 0
    assert out["unpriced_models"] == ["mystery-model"]


def test_partial_window_states_the_unpriced_share() -> None:
    client = _client(calls=5, unpriced_calls=2, unpriced=[("mystery-model", 2)])
    with patch.object(clickhouse_store, "_get_client", return_value=client):
        out = clickhouse_store.cost_report(days=7)
    assert out["totals"]["cost_usd"] == 0.0
    assert out["priced_calls"] == 3 and out["unpriced_calls"] == 2


def test_fully_priced_window_is_unchanged() -> None:
    client = _client(calls=4, unpriced_calls=0, unpriced=[])
    with patch.object(clickhouse_store, "_get_client", return_value=client):
        out = clickhouse_store.cost_report(days=7)
    assert out["pricing_coverage_checked"] is True
    assert out["unpriced_models"] == [] and out["unpriced_calls"] == 0


def test_unpriced_models_survive_the_per_model_limit() -> None:
    """Unpriced rows cost 0 so sort last under LIMIT 50; they need their own query."""
    client = _client(calls=60, unpriced_calls=1, unpriced=[("late-model", 1)])
    with patch.object(clickhouse_store, "_get_client", return_value=client):
        out = clickhouse_store.cost_report(days=7)
    assert out["unpriced_models"] == ["late-model"]


pytest.importorskip("fastapi")


@pytest.fixture
def app_client(tmp_path: Any) -> Any:
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(token=TOKEN, capsule_dir=capsules, static_mounted_by_caller=True)
    with TestClient(app) as c:
        yield c


def _get(c: Any) -> dict[str, Any]:
    return c.get(f"/api/cost/report?token={TOKEN}", headers=HEADERS).json()


def test_endpoint_refuses_an_all_unpriced_clickhouse_window(
    app_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://127.0.0.1:1/nova")
    client = _client(calls=3, unpriced_calls=3, unpriced=[("mystery-model", 3)])
    with patch.object(clickhouse_store, "_get_client", return_value=client):
        body = _get(app_client)
    agg = body["aggregate"]
    assert agg["computable"] is False
    assert agg["condition"] == "absent_contributor"
    assert "value" not in agg
    assert body["totals"]["cost_usd"] is None


def test_endpoint_flags_a_partial_clickhouse_window_as_lower_bound(
    app_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://127.0.0.1:1/nova")
    client = _client(calls=5, unpriced_calls=2, unpriced=[("mystery-model", 2)])
    with patch.object(clickhouse_store, "_get_client", return_value=client):
        agg = _get(app_client)["aggregate"]
    assert agg["computable"] is True
    notes = agg["notes"]
    assert notes["cost_is_lower_bound"] is True
    assert notes["unpriced_models"] == ["mystery-model"]
    assert notes["pricing_coverage_checked"] is True
