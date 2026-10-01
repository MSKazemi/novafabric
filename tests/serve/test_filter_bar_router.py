"""Dashboard filter bar over HTTP — ADR-0232 D1/D3, ADR-0233 scope, ADR-0234 D2.

Acceptance criteria pinned here:

* ``/api/filter/parse`` compiles a bar string to the DSL's own predicates and
  rejects anything ``nova query --where`` would reject (422, vocabulary named).
* ``/api/filter/runs`` returns exactly the runs ``nova query`` would count for the
  same predicates, newest first, bounded, with ``truncated`` / ``complete`` stated.
* ``tree`` / ``root`` scope widen a match to its capsule tree (ADR-0233).
* ``/api/filter/suggest`` suggests observed values only, bounded, with a
  ``truncated`` flag (D3) and an unknown dimension is a 422, not an empty list.
* Every route requires the token (401) and is classified ``read`` (ADR-0228).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.query import parse_filter_bar, run_query  # noqa: E402
from novafabric.query.parser import validate_query_object  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.authz import ROUTE_SCOPES, Scope  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321", "Authorization": f"Bearer {VALID_TOKEN}"}


def _write_capsule(
    base: Path,
    run_id: str,
    *,
    status: str = "success",
    created_at: str = "2026-09-20T00:00:00+00:00",
    model: str | None = None,
    parent: str | None = None,
) -> None:
    cdir = base / run_id
    cdir.mkdir()
    manifest: dict[str, object] = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.102.1",
        "run_id": run_id,
        "created_at": created_at,
        "finished_at": created_at,
        "duration_ms": 10,
        "command": ["python", "-c", "print(1)"],
        "exit_code": 0,
        "status": status,
        "capture_mode": "cli-wrapper",
        "model_call_count": 1 if model else 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    if parent:
        manifest["parent_run_id"] = parent
        manifest["capsule_type"] = "worker"
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    calls = (
        [{"gen_ai.response.model": model, "gen_ai.usage.input_tokens": 1, "duration_ms": 5}]
        if model
        else []
    )
    (cdir / "model-calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    _write_capsule(base, "OLD-OK", created_at="2026-09-18T00:00:00+00:00", model="gpt-4o")
    _write_capsule(base, "NEW-ERR", status="error", created_at="2026-09-21T00:00:00+00:00",
                   model="claude-x")
    _write_capsule(base, "MID-ERR", status="error", created_at="2026-09-19T00:00:00+00:00",
                   model="gpt-4o")
    # A two-capsule tree: only the worker fails.
    _write_capsule(base, "ROOT", created_at="2026-09-17T00:00:00+00:00")
    _write_capsule(base, "WORKER", status="error", created_at="2026-09-17T00:00:01+00:00",
                   parent="ROOT")
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


# ---------- auth + authz ----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/filter/parse?f=status:error", "/api/filter/runs", "/api/filter/suggest?dimension=model"],
)
def test_every_filter_route_requires_the_token(client: TestClient, path: str) -> None:
    resp = client.get(path, headers={"host": "127.0.0.1:4321"})
    assert resp.status_code == 401


def test_filter_routes_are_classified_read() -> None:
    for path in ("/api/filter/parse", "/api/filter/runs", "/api/filter/suggest"):
        assert ROUTE_SCOPES[("GET", path)] is Scope.read


# ---------- parse -------------------------------------------------------------


def test_parse_compiles_to_dsl_predicates_and_cli(client: TestClient) -> None:
    resp = client.get("/api/filter/parse", params={"f": "status:error -model:gpt-4o"},
                      headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["predicates"] == ["status = error", "model != gpt-4o"]
    assert body["where"] == "status = error AND model != gpt-4o"
    assert body["cli_equivalent"] == (
        "nova query --select 'count()' --where 'status = error AND model != gpt-4o'"
    )


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("cost:>0.5", "metric"),
        ("model:gpt-4*", "pattern"),
        ("nonsense", "cannot parse"),
        ('asset:"unterminated', "unbalanced quote"),
    ],
)
def test_parse_rejects_what_the_dsl_rejects(client: TestClient, text: str, needle: str) -> None:
    resp = client.get("/api/filter/parse", params={"f": text}, headers=HEADERS)
    assert resp.status_code == 422
    assert needle in resp.json()["detail"]


def test_parse_bounds_the_input(client: TestClient) -> None:
    resp = client.get("/api/filter/parse", params={"f": "status:x " * 400}, headers=HEADERS)
    assert resp.status_code == 422
    assert "longer than" in resp.json()["detail"]


# ---------- runs --------------------------------------------------------------


def test_runs_match_and_order_newest_first(client: TestClient) -> None:
    resp = client.get("/api/filter/runs", params={"f": "status:error"}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    ids = [r["run_id"] for r in body["items"]]
    assert ids == ["NEW-ERR", "MID-ERR", "WORKER"]
    assert body["matched"] == 3
    assert body["truncated"] is False
    assert body["complete"] is True
    assert body["items"][0]["status"] == "error"
    assert body["items"][0]["command"] == ["python", "-c", "print(1)"]


def test_runs_selection_agrees_with_nova_query(client: TestClient, capsule_dir: Path) -> None:
    """D1's ceiling, end to end: the bar selects what `nova query` counts."""
    text = "status:error model:gpt-4o"
    resp = client.get("/api/filter/runs", params={"f": text}, headers=HEADERS)
    ids = {r["run_id"] for r in resp.json()["items"]}

    where = " AND ".join(p.normalized() for p in parse_filter_bar(text))
    plan = validate_query_object({"select": ["count()"], "where": where})
    counted = run_query(plan, capsule_dir)["rows"][0]["count()"]
    assert ids == {"MID-ERR"}
    assert counted == len(ids)


def test_runs_limit_reports_truncation(client: TestClient) -> None:
    resp = client.get("/api/filter/runs", params={"f": "status:error", "limit": 1},
                      headers=HEADERS)
    body = resp.json()
    assert [r["run_id"] for r in body["items"]] == ["NEW-ERR"]
    assert body["matched"] == 3
    assert body["truncated"] is True
    assert body["complete"] is False


def test_tree_scope_widens_to_the_whole_tree(client: TestClient) -> None:
    node = client.get("/api/filter/runs", params={"f": "status:error", "since": "2026-09-16T00:00:00Z",
                                                  "until": "2026-09-17T12:00:00Z"},
                      headers=HEADERS).json()
    assert [r["run_id"] for r in node["items"]] == ["WORKER"]

    tree = client.get("/api/filter/runs", params={"f": "status:error", "scope": "tree",
                                                  "since": "2026-09-16T00:00:00Z",
                                                  "until": "2026-09-17T12:00:00Z"},
                      headers=HEADERS).json()
    assert {r["run_id"] for r in tree["items"]} == {"ROOT", "WORKER"}
    assert tree["scope"] == "tree"
    assert "--scope tree" in tree["cli_equivalent"]

    root = client.get("/api/filter/runs", params={"f": "status:error", "scope": "root",
                                                  "since": "2026-09-16T00:00:00Z",
                                                  "until": "2026-09-17T12:00:00Z"},
                      headers=HEADERS).json()
    assert [r["run_id"] for r in root["items"]] == ["ROOT"]


def test_runs_rejects_unknown_scope_and_bad_window(client: TestClient) -> None:
    bad_scope = client.get("/api/filter/runs", params={"scope": "galaxy"}, headers=HEADERS)
    assert bad_scope.status_code == 422
    assert "node" in bad_scope.json()["detail"]
    bad_since = client.get("/api/filter/runs", params={"since": "yesterday-ish"},
                           headers=HEADERS)
    assert bad_since.status_code == 422


def test_runs_on_missing_store_is_empty_not_500(tmp_path: Path) -> None:
    app = create_app(token=VALID_TOKEN, capsule_dir=tmp_path / "absent",
                     db_path=tmp_path / "r.db", static_dir=None)
    with TestClient(app) as c:
        resp = c.get("/api/filter/runs", params={"f": "status:error"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["complete"] is True


# ---------- suggest -----------------------------------------------------------


def test_suggest_returns_observed_values_in_window(client: TestClient) -> None:
    resp = client.get("/api/filter/suggest",
                      params={"dimension": "model", "since": "2026-09-01T00:00:00Z"},
                      headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["values"] == ["claude-x", "gpt-4o"]
    assert body["truncated"] is False
    assert body["since"].startswith("2026-09-01")


def test_suggest_flags_truncation(client: TestClient) -> None:
    resp = client.get("/api/filter/suggest",
                      params={"dimension": "model", "since": "2026-09-01T00:00:00Z", "limit": 1},
                      headers=HEADERS)
    body = resp.json()
    assert len(body["values"]) == 1
    assert body["truncated"] is True


def test_suggest_unknown_dimension_is_422(client: TestClient) -> None:
    resp = client.get("/api/filter/suggest", params={"dimension": "cost"}, headers=HEADERS)
    assert resp.status_code == 422
    assert "allowed" in resp.json()["detail"]
