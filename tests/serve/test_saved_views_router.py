"""ADR-0232 D4 — a dashboard saved view *is* an ADR-0130 ``nova view``.

Acceptance criteria:

* saving from the dashboard writes a view file ``nova view`` reads — same
  directory, same envelope, validated by the same fail-closed parser;
* the stored query is the filter's own predicates (plus the status chip as a
  ``status`` predicate), the scope and the window; sort is advisory display;
* the CLI can run what the dashboard saved (``nova view run``);
* a view the CLI saved is offered back to the dashboard when the filter bar can
  express it, and listed with the reason when it cannot — never approximated;
* free-text search is not a query and is not saved; an invalid filter is a 422
  and writes nothing; an existing name is a 409 unless overwriting;
* writes are ``operate`` scope and audited; delete removes the file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import audit  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.views import list_views, load_view  # noqa: E402

TOKEN = "views-token-0123456789abcdef"
H = {"host": "127.0.0.1:4321"}
Q = f"token={TOKEN}"


@pytest.fixture
def views_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "views"
    monkeypatch.setenv("NOVAFABRIC_VIEWS_DIR", str(d))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    return d


@pytest.fixture
def client(tmp_path: Path, views_dir: Path) -> Iterator[TestClient]:
    base = tmp_path / "runs"
    base.mkdir()
    app = create_app(token=TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None)
    yield TestClient(app)


def _save(c: TestClient, **body: object) -> object:
    return c.post(f"/api/views?{Q}", json=body, headers=H)


def test_saving_writes_a_nova_view_the_cli_reads(client: TestClient, views_dir: Path) -> None:
    r = _save(
        client, name="Failing GPT runs", f="model:gpt-4o", status="error",
        scope="tree", since="2026-09-01", sort="oldest",
    )
    assert r.status_code == 200, r.text  # type: ignore[attr-defined]
    view = load_view("failing-gpt-runs", views_dir)
    assert view.query == {
        "select": ["count()"],
        "where": ["model = gpt-4o", "status = error"],
        "scope": "tree",
        "since": "2026-09-01",
    }
    assert view.tags == ["dashboard:runs"]
    assert view.display is not None and view.display.sort is not None
    assert (view.display.sort[0].field, view.display.sort[0].order) == ("created_at", "asc")
    assert (view.created_by or "").startswith("dashboard:")


def test_the_cli_runs_what_the_dashboard_saved(client: TestClient, views_dir: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.view import view_app

    _save(client, name="errors", f="status:error")
    result = CliRunner().invoke(view_app, ["show", "errors", "--views-dir", str(views_dir)])
    assert result.exit_code == 0, result.output
    assert "status = error" in result.output


def test_listing_round_trips_the_runs_view_state(client: TestClient) -> None:
    _save(client, name="mine", f='asset:"my agent" -model:gpt-4o', scope="root", sort="longest")
    body = client.get(f"/api/views?{Q}", headers=H).json()
    (view,) = body["views"]
    assert view["dashboard"] == {
        "f": 'asset:"my agent" -model:gpt-4o',
        "scope": "root",
        "since": "",
        "until": "",
        "status": "all",
        "sort": "longest",
    }
    assert view["cli_equivalent"] == "nova view run mine"
    assert view["view_hash"].startswith("sha256:")


def test_a_cli_view_the_bar_cannot_express_is_listed_with_the_reason(
    client: TestClient, views_dir: Path
) -> None:
    from novafabric.views import SavedView, save_view

    save_view(
        SavedView(
            view_id="two-models",
            name="two models",
            query={"select": ["count()"], "where": ["model IN (a, b)"]},
            created_at="2026-10-01T00:00:00Z",
        ),
        views_dir,
    )
    (view,) = client.get(f"/api/views?{Q}", headers=H).json()["views"]
    assert view["dashboard"] is None
    assert "nova view run two-models" in view["dashboard_unavailable_reason"]


def test_invalid_filter_is_422_and_writes_nothing(client: TestClient, views_dir: Path) -> None:
    r = _save(client, name="bad", f="cost:>0.5")
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert list_views(views_dir) == ([], [])


def test_an_existing_name_is_409_unless_overwriting(client: TestClient, views_dir: Path) -> None:
    assert _save(client, name="dup", f="status:error").status_code == 200  # type: ignore[attr-defined]
    assert _save(client, name="dup", f="status:success").status_code == 409  # type: ignore[attr-defined]
    assert _save(client, name="dup", f="status:success", overwrite=True).status_code == 200  # type: ignore[attr-defined]
    assert load_view("dup", views_dir).query["where"] == ["status = success"]


def test_search_text_is_not_part_of_the_contract(client: TestClient) -> None:
    r = _save(client, name="with search", q="abc")
    # Unknown body keys are ignored by the model; nothing about `q` is stored.
    assert r.status_code == 200  # type: ignore[attr-defined]
    assert "q" not in r.json()["view"]["query"]  # type: ignore[attr-defined]


def test_delete_removes_the_file_and_both_writes_are_audited(
    client: TestClient, views_dir: Path
) -> None:
    _save(client, name="gone soon", f="status:error")
    r = client.delete(f"/api/views/gone-soon?{Q}", headers=H)
    assert r.status_code == 200, r.text
    assert list_views(views_dir) == ([], [])
    assert client.delete(f"/api/views/gone-soon?{Q}", headers=H).status_code == 404
    actions = [rec["action"] for rec in audit.read_recent(10)]
    assert "view_save" in actions and "view_delete" in actions


def test_routes_are_classified() -> None:
    from novafabric.serve.authz import ROUTE_SCOPES, Scope

    assert ROUTE_SCOPES[("GET", "/api/views")] is Scope.read
    assert ROUTE_SCOPES[("POST", "/api/views")] is Scope.operate
    assert ROUTE_SCOPES[("DELETE", "/api/views/{view_id}")] is Scope.operate
