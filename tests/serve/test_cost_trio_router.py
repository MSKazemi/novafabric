"""Tests for the cost-analytics-trio dashboard read surface (ADR-0201 P6).

Three POST endpoints wrap the pure ``nova cost`` cores given a request
document instead of a file path:

- ``POST /api/cost/attribute``       → productive-vs-wasted spend (ADR-0146 D3)
- ``POST /api/cost/fairness``        → per-agent share/Gini (ADR-0146 D5)
- ``POST /api/cost/usage-breakdown`` → token usage-type composition (ADR-0132)

Each mirrors its CLI command's document shape and error semantics. See
src/novafabric/serve/routers/cost_trio.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    base = tmp_path / "runs"
    base.mkdir()
    app = create_app(
        token=VALID_TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


class TestAuth:
    def test_attribute_requires_token(self, client: TestClient) -> None:
        res = client.post("/api/cost/attribute", json={"runs": []}, headers=HEADERS)
        assert res.status_code == 401

    def test_fairness_requires_token(self, client: TestClient) -> None:
        res = client.post("/api/cost/fairness", json={"totals": {}}, headers=HEADERS)
        assert res.status_code == 401

    def test_usage_breakdown_requires_token(self, client: TestClient) -> None:
        res = client.post(
            "/api/cost/usage-breakdown", json={"usage_totals": {}}, headers=HEADERS
        )
        assert res.status_code == 401


class TestAttribute:
    def test_productive_vs_wasted(self, client: TestClient) -> None:
        res = client.post(
            f"/api/cost/attribute?{TOKEN_Q}",
            headers=HEADERS,
            json={
                "runs": [
                    {"run_id": "r1", "status": "success", "cost": 3.0},
                    {"run_id": "r2", "status": "failed", "cost": 1.0},
                ]
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total_spend"] == 4.0
        assert body["productive_spend"] == 3.0
        assert body["wasted_spend"] == 1.0
        assert body["wasted_fraction"] == 0.25
        assert body["by_status"] == {"failed": 1.0, "success": 3.0}

    def test_custom_productive_statuses(self, client: TestClient) -> None:
        res = client.post(
            f"/api/cost/attribute?{TOKEN_Q}",
            headers=HEADERS,
            json={
                "runs": [{"run_id": "r1", "status": "partial", "cost": 2.0}],
                "productive_statuses": ["partial"],
            },
        )
        assert res.status_code == 200
        assert res.json()["wasted_spend"] == 0.0

    def test_empty_runs_all_zero(self, client: TestClient) -> None:
        res = client.post(
            f"/api/cost/attribute?{TOKEN_Q}", headers=HEADERS, json={"runs": []}
        )
        assert res.status_code == 200
        assert res.json()["total_spend"] == 0.0
        assert res.json()["wasted_fraction"] == 0.0

    def test_bad_run_is_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/cost/attribute?{TOKEN_Q}",
            headers=HEADERS,
            json={"runs": [{"status": "success", "cost": 1.0}]},  # missing run_id
        )
        assert res.status_code == 422


class TestFairness:
    def test_report_shape(self, client: TestClient) -> None:
        res = client.post(
            f"/api/cost/fairness?{TOKEN_Q}",
            headers=HEADERS,
            json={"totals": {"cost": {"a": 3.0, "b": 1.0}}},
        )
        assert res.status_code == 200
        metrics = res.json()["metrics"]
        assert len(metrics) == 1
        assert metrics[0]["dimension"] == "cost"
        assert set(metrics[0]["shares"]) == {"a", "b"}
        assert abs(sum(metrics[0]["shares"].values()) - 1.0) < 1e-9
        assert metrics[0]["gini"] >= 0.0

    def test_dimensions_sorted(self, client: TestClient) -> None:
        res = client.post(
            f"/api/cost/fairness?{TOKEN_Q}",
            headers=HEADERS,
            json={"totals": {"energy": {"a": 1.0}, "cost": {"a": 1.0}}},
        )
        assert res.status_code == 200
        dims = [m["dimension"] for m in res.json()["metrics"]]
        assert dims == ["cost", "energy"]


class TestUsageBreakdown:
    def test_composition(self, client: TestClient) -> None:
        res = client.post(
            f"/api/cost/usage-breakdown?{TOKEN_Q}",
            headers=HEADERS,
            json={"usage_totals": {"input_tokens": 60, "output_tokens": 40}},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["counted_tokens"] == 100
        assert abs(sum(body["composition"].values()) - 1.0) < 1e-9

    def test_empty_totals_safe(self, client: TestClient) -> None:
        res = client.post(
            f"/api/cost/usage-breakdown?{TOKEN_Q}",
            headers=HEADERS,
            json={"usage_totals": {}},
        )
        assert res.status_code == 200
        assert res.json()["counted_tokens"] == 0
