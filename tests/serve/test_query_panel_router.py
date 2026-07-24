"""Tests for the dashboard query panel (ADR-0129 read surface, ADR-0183 pattern).

``POST /api/query`` accepts ``{q, engine?}`` where ``q`` is a Capsule Query DSL
document (the same JSON/YAML shape ``nova query --query-file`` accepts) and
executes it via :func:`novafabric.query.executor.run_query` — no server-side
SQL, no subprocess. See src/novafabric/serve/routers/query_panel.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import routers as _routers_pkg  # noqa: E402, F401
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.routers import query_panel  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


def _write_capsule(
    base: Path,
    run_id: str,
    *,
    status: str = "success",
    calls: list[dict] | None = None,
) -> None:
    cdir = base / run_id
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.63.0",
        "run_id": run_id,
        "created_at": "2026-07-24T00:00:00+00:00",
        "finished_at": "2026-07-24T00:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print(1)"],
        "exit_code": 0,
        "status": status,
        "capture_mode": "cli-wrapper",
        "model_call_count": len(calls or []),
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    lines = [json.dumps(c) for c in (calls or [])]
    (cdir / "model-calls.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    _write_capsule(
        base,
        "RUN1",
        calls=[
            {
                "gen_ai.response.model": "gpt-4o-mini",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
                "nova.cost": {"amount": 0.01, "currency": "USD"},
                "duration_ms": 120,
            }
        ],
    )
    _write_capsule(base, "RUN2", status="failed")
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


def _post(client: TestClient, q: str, engine: str | None = None) -> "TestClient":
    body: dict = {"q": q}
    if engine is not None:
        body["engine"] = engine
    return client.post(f"/api/query?{TOKEN_Q}", json=body, headers=HEADERS)


class TestAuth:
    def test_requires_token(self, client: TestClient) -> None:
        res = client.post("/api/query", json={"q": '{"select": ["count()"]}'}, headers=HEADERS)
        assert res.status_code == 401


class TestHappyPath:
    def test_count_query_json(self, client: TestClient) -> None:
        res = _post(client, json.dumps({"select": ["count()"]}))
        assert res.status_code == 200
        body = res.json()
        assert body["row_count"] == 1
        assert body["rows"][0]["count()"] == 2
        assert body["truncated"] is False
        assert "cli_equivalent" in body
        assert body["cli_equivalent"].startswith("nova query")

    def test_yaml_document_accepted(self, client: TestClient) -> None:
        yaml_text = "select:\n  - count()\nwhere: 'status = success'\n"
        res = _post(client, yaml_text)
        assert res.status_code == 200
        assert res.json()["rows"][0]["count()"] == 1

    def test_group_by_and_aggregate(self, client: TestClient) -> None:
        res = _post(
            client,
            json.dumps({"select": ["avg(cost) AS avg_cost", "count()"], "group_by": ["status"]}),
        )
        assert res.status_code == 200
        body = res.json()
        statuses = {r["status"] for r in body["rows"]}
        assert statuses == {"success", "failed"}

    def test_engine_override(self, client: TestClient) -> None:
        res = _post(client, json.dumps({"select": ["count()"]}), engine="sqlite")
        assert res.status_code == 200
        assert res.json()["index"]["engine"] == "sqlite"


class TestErrors:
    def test_empty_q_is_422(self, client: TestClient) -> None:
        res = _post(client, "")
        assert res.status_code == 422

    def test_malformed_document_is_422(self, client: TestClient) -> None:
        res = _post(client, "{not: valid: json::")
        assert res.status_code == 422

    def test_non_object_document_is_422(self, client: TestClient) -> None:
        res = _post(client, json.dumps([1, 2, 3]))
        assert res.status_code == 422

    def test_unknown_clause_is_422(self, client: TestClient) -> None:
        res = _post(client, json.dumps({"select": ["count()"], "raw_sql": "DROP TABLE x"}))
        assert res.status_code == 422
        assert "raw_sql" in res.json()["detail"]

    def test_unknown_filter_field_is_422(self, client: TestClient) -> None:
        res = _post(client, json.dumps({"select": ["count()"], "where": "secret_field = 1"}))
        assert res.status_code == 422

    def test_missing_select_is_422(self, client: TestClient) -> None:
        res = _post(client, json.dumps({"where": "status = success"}))
        assert res.status_code == 422

    def test_invalid_engine_is_422(self, client: TestClient) -> None:
        res = _post(client, json.dumps({"select": ["count()"]}), engine="postgres")
        assert res.status_code == 422


class TestRowCap:
    def test_result_rows_capped_at_5000(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        big_rows = [{"status": f"s{i}", "count()": 1} for i in range(6000)]

        def _fake_run_query(plan, base, *, engine=None, now=None):  # noqa: ANN001, ARG001
            return {
                "schema_version": "0.1.0",
                "generated_at": "2026-07-24T00:00:00Z",
                "query": plan.to_query_object(),
                "time_window": {"since": "1970-01-01T00:00:00Z", "until": "2026-07-24T00:00:00Z"},
                "columns": ["status", "count()"],
                "rows": big_rows,
                "row_count": len(big_rows),
                "truncated": False,
                "index": {"engine": "sqlite", "built_at": "now", "capsule_count": 6000},
            }

        monkeypatch.setattr(query_panel, "run_query", _fake_run_query)
        res = _post(client, json.dumps({"select": ["count()"], "group_by": ["status"]}))
        assert res.status_code == 200
        body = res.json()
        assert len(body["rows"]) == 5000
        assert body["row_count"] == 5000
        assert body["truncated"] is True
