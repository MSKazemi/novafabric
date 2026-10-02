"""The Dashboards view's read surface — ADR-0235 widgets, ADR-0236 ``ratio()``.

What each test protects:

* one invalid file is **reported by name**, never allowed to blank the listing
  and never silently dropped;
* a dashboard reference is ``ok`` / ``missing`` / ``invalid`` — a partial
  dashboard never reads as a whole one;
* export is the bytes on disk, so unknown fields survive (D6);
* a widget's data carries the ratio's operands, and an undefined ratio is
  ``None``, not ``0`` (ADR-0236 D4) while a measured zero stays ``0``;
* nothing here writes: a missing dashboards directory is not created.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.authz import ROUTE_SCOPES, Scope  # noqa: E402
from novafabric.serve.introspect import iter_routes  # noqa: E402
from novafabric.serve.routers.dashboards import (  # noqa: E402
    DashboardDetailResponse,
    DashboardListResponse,
    WidgetDataResponse,
)

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321", "Authorization": f"Bearer {VALID_TOKEN}"}


def _capsule(base: Path, run_id: str, *, status: str, calls: list[dict[str, Any]]) -> None:
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
        "model_call_count": len(calls),
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    lines = [json.dumps(c) for c in calls]
    (cdir / "model-calls.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))


def _widget(widget_id: str, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "$novafabricWidget": True,
        "version": 1,
        "id": widget_id,
        "title": f"Widget {widget_id}",
        "query": {"select": ["count()"], "group_by": ["status"], "since": "3650d"},
        "presentation": {"chart": "bar"},
    }
    doc.update(overrides)
    return doc


def _write(root: Path, name: str, doc: Any) -> Path:
    path = root / name
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    return home


@pytest.fixture
def root(home: Path) -> Path:
    root = home / "dashboards"
    root.mkdir()
    return root


@pytest.fixture
def client(tmp_path: Path, home: Path) -> Iterator[TestClient]:
    base = tmp_path / "runs"
    base.mkdir()
    _capsule(
        base,
        "RUN1",
        status="success",
        calls=[
            {
                "gen_ai.response.model": "gpt-4o-mini",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
                "nova.cost": {"amount": 0.02, "currency": "USD"},
                "duration_ms": 120,
            }
        ],
    )
    _capsule(base, "RUN2", status="failed", calls=[])
    app = create_app(
        token=VALID_TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


def _get(client: TestClient, path: str) -> Any:
    return client.get(path, headers=HEADERS)


# ---------------------------------------------------------------------------
# Classification + auth
# ---------------------------------------------------------------------------

_ROUTES = (
    "/api/dashboards",
    "/api/dashboards/{dashboard_id}",
    "/api/dashboards/{dashboard_id}/export",
    "/api/dashboard-widgets/{widget_id}/data",
    "/api/dashboard-widgets/{widget_id}/export",
)


def test_every_route_is_read_scoped_and_mounted(tmp_path: Path) -> None:
    for path in _ROUTES:
        assert ROUTE_SCOPES[("GET", path)] is Scope.read
    app = create_app(
        token=VALID_TOKEN, capsule_dir=tmp_path, db_path=tmp_path / "r.db", static_dir=None
    )
    mounted = {info.path for info in iter_routes(app)}
    assert set(_ROUTES) <= mounted


def test_requires_a_token(client: TestClient) -> None:
    res = client.get("/api/dashboards", headers={"host": "127.0.0.1:4321"})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_missing_directory_lists_empty_and_is_not_created(client: TestClient, home: Path) -> None:
    res = _get(client, "/api/dashboards")
    assert res.status_code == 200
    body = res.json()
    DashboardListResponse.model_validate(body)
    assert body["dashboards"] == [] and body["widgets"] == [] and body["invalid_files"] == []
    assert body["cli_equivalent"] == "nova dashboard list"
    assert not (home / "dashboards").exists(), "a GET must not create state"


def test_one_invalid_file_is_reported_not_fatal(client: TestClient, root: Path) -> None:
    _write(root, "good.widget.json", _widget("good"))
    _write(root, "evil.widget.json", _widget("evil", query={"sql": "DROP TABLE runs"}))
    (root / "broken.dashboard.json").write_text("{not json")
    body = _get(client, "/api/dashboards").json()
    assert [w["id"] for w in body["widgets"]] == ["good"]
    refused = {f["file"]: f for f in body["invalid_files"]}
    assert set(refused) == {"evil.widget.json", "broken.dashboard.json"}
    assert refused["evil.widget.json"]["kind"] == "widget"
    assert "DSL rejects" in refused["evil.widget.json"]["error"]
    assert refused["broken.dashboard.json"]["kind"] == "dashboard"


def test_listing_counts_missing_and_invalid_references(client: TestClient, root: Path) -> None:
    _write(root, "ok.widget.json", _widget("ok"))
    _write(root, "bad.widget.json", _widget("bad", version=99))
    _write(
        root,
        "ops.dashboard.json",
        {
            "$novafabricDashboard": True,
            "version": 1,
            "id": "ops",
            "title": "Ops",
            "widgets": [{"widget": "ok"}, {"widget": "gone"}, {"widget": "bad"}],
        },
    )
    (dash,) = _get(client, "/api/dashboards").json()["dashboards"]
    assert dash["widget_count"] == 3
    assert dash["unresolved_widgets"] == ["gone"]
    assert dash["invalid_widgets"] == ["bad"]


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


def test_detail_resolves_each_reference_with_a_status(client: TestClient, root: Path) -> None:
    _write(root, "ok.widget.json", _widget("ok", description="per status"))
    _write(root, "bad.widget.json", _widget("bad", version=99))
    _write(
        root,
        "ops.dashboard.json",
        {
            "$novafabricDashboard": True,
            "version": 1,
            "id": "ops",
            "title": "Ops",
            "widgets": [
                {"widget": "ok", "position": {"x": 0, "y": 0, "w": 6, "h": 4}},
                {"widget": "gone"},
                {"widget": "bad"},
            ],
        },
    )
    res = _get(client, "/api/dashboards/ops")
    assert res.status_code == 200, res.text
    body = res.json()
    DashboardDetailResponse.model_validate(body)
    by_id = {ref["widget"]: ref for ref in body["widgets"]}
    assert [ref["widget"] for ref in body["widgets"]] == ["ok", "gone", "bad"]
    assert by_id["ok"]["status"] == "ok"
    assert by_id["ok"]["position"] == {"x": 0, "y": 0, "w": 6, "h": 4}
    assert by_id["ok"]["definition"]["description"] == "per status"
    assert by_id["gone"]["status"] == "missing" and by_id["gone"]["definition"] is None
    assert by_id["bad"]["status"] == "invalid"
    assert "version 99" in by_id["bad"]["error"]
    assert body["cli_equivalent"] == "nova dashboard show ops"


def test_detail_404_and_422(client: TestClient, root: Path) -> None:
    assert _get(client, "/api/dashboards/nope").status_code == 404
    _write(root, "odd.dashboard.json", {"$novafabricDashboard": True, "version": 1, "id": "odd"})
    res = _get(client, "/api/dashboards/odd")
    assert res.status_code == 422
    assert "invalid" in res.json()["detail"]


@pytest.mark.parametrize("bad_id", ["..", ".hidden", "UPPER", "a%2Fb"])
def test_ids_outside_the_schema_pattern_are_refused(client: TestClient, bad_id: str) -> None:
    assert _get(client, f"/api/dashboards/{bad_id}").status_code in (404, 422)
    assert _get(client, f"/api/dashboard-widgets/{bad_id}/data").status_code in (404, 422)


# ---------------------------------------------------------------------------
# Export — the bytes on disk (D6)
# ---------------------------------------------------------------------------


def test_widget_export_round_trips_unknown_fields(client: TestClient, root: Path) -> None:
    doc = _widget("w1", x_future_setting={"keep": True})
    path = root / "w1.widget.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    res = _get(client, "/api/dashboard-widgets/w1/export")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert 'filename="w1.widget.json"' in res.headers["content-disposition"]
    assert res.content == path.read_bytes()
    assert json.loads(res.content)["x_future_setting"] == {"keep": True}


def test_dashboard_export_and_missing(client: TestClient, root: Path) -> None:
    doc = {
        "$novafabricDashboard": True,
        "version": 1,
        "id": "ops",
        "title": "Ops",
        "widgets": [],
        "x_newer": 1,
    }
    _write(root, "ops.dashboard.json", doc)
    res = _get(client, "/api/dashboards/ops/export")
    assert res.status_code == 200
    assert json.loads(res.content) == doc
    assert 'filename="ops.dashboard.json"' in res.headers["content-disposition"]
    assert _get(client, "/api/dashboards/none/export").status_code == 404
    assert _get(client, "/api/dashboard-widgets/none/export").status_code == 404


# ---------------------------------------------------------------------------
# Data — the widget's own query, with ratio operands named
# ---------------------------------------------------------------------------


def test_widget_data_runs_the_stored_query(client: TestClient, root: Path) -> None:
    _write(root, "w.widget.json", _widget("w"))
    res = _get(client, "/api/dashboard-widgets/w/data")
    assert res.status_code == 200, res.text
    body = res.json()
    WidgetDataResponse.model_validate(body)
    counts = {row["status"]: row["count()"] for row in body["rows"]}
    assert counts == {"success": 1, "failed": 1}
    assert body["widget"]["chart"] == "bar"
    assert body["derived"] == []
    assert body["cli_equivalent"].startswith("nova query")


def test_ratio_names_its_operands_and_undefined_is_not_zero(
    client: TestClient, root: Path
) -> None:
    query = {
        "select": ["count()", "sum(cost)", "ratio(sum(cost), count()) AS cost_per_run"],
        "group_by": ["status"],
        "since": "3650d",
    }
    _write(root, "r.widget.json", _widget("r", query=query, presentation={"chart": "table"}))
    body = _get(client, "/api/dashboard-widgets/r/data").json()
    assert body["derived"] == [
        {
            "alias": "cost_per_run",
            "func": "ratio",
            "numerator": "sum(cost)",
            "denominator": "count()",
        }
    ]
    rows = {row["status"]: row for row in body["rows"]}
    assert rows["success"]["cost_per_run"] == pytest.approx(0.02)
    # RUN2 has no cost rows: an absent numerator is no value, never 0.0.
    assert rows["failed"]["sum(cost)"] is None
    assert rows["failed"]["cost_per_run"] is None


def test_widget_data_404_and_refusal(client: TestClient, root: Path) -> None:
    assert _get(client, "/api/dashboard-widgets/none/data").status_code == 404
    _write(root, "new.widget.json", _widget("new", version=2))
    res = _get(client, "/api/dashboard-widgets/new/data")
    assert res.status_code == 422
    assert "Refusing rather than guessing" in res.json()["detail"]
