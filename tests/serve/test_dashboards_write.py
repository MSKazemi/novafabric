"""The Dashboards view's write path — validate (dry run) then apply.

Acceptance criteria pinned here:

* ``POST /api/dashboards/validate`` never writes (not even the directory) and
  returns the server's verdict: the normalised bytes that *would* be stored,
  what is on disk now, and create / update / unchanged;
* ``POST /api/dashboards/apply`` stores **exactly the bytes** ``nova dashboard
  apply`` would — it calls the same loaders and the same store;
* refusals carry the validator's reason: schema first, then the ADR-0129 DSL
  allow-list (ADR-0235 D7) — and write nothing;
* an id is path-constrained, so no document can reach outside the directory;
* an oversize body is refused before it is parsed;
* re-applying the same document is a no-op (no write, no mtime churn);
* a write that fails part-way leaves neither a partial file nor a temp file;
* ``apply`` is ``operate`` scope, ``validate`` is ``read``; every outcome of
  ``apply`` — written, unchanged, refused — is in the audit log, without the
  document body;
* a stale preview (``base_sha256``) is a 409, not a silent overwrite.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.dashboards import DashboardStore, apply_path  # noqa: E402
from novafabric.serve import audit, token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.authz import ROUTE_SCOPES, Scope  # noqa: E402
from novafabric.serve.introspect import iter_routes  # noqa: E402

TOKEN = "test-token-1234567890abcdef"
H = {"host": "127.0.0.1:4321", "Authorization": f"Bearer {TOKEN}"}
VALIDATE = "/api/dashboards/validate"
APPLY = "/api/dashboards/apply"


def _widget(widget_id: str = "w1", **overrides: Any) -> dict[str, Any]:
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


def _dashboard(dash_id: str = "ops", widgets: list[str] | None = None, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "$novafabricDashboard": True,
        "version": 1,
        "id": dash_id,
        "title": "Ops",
        "widgets": [{"widget": w} for w in (widgets or [])],
    }
    doc.update(over)
    return doc


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".novafabric").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    return home


@pytest.fixture
def root(home: Path) -> Path:
    return home / "dashboards"


@pytest.fixture
def client(tmp_path: Path, home: Path) -> Iterator[TestClient]:
    base = tmp_path / "runs"
    base.mkdir()
    app = create_app(token=TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None)
    with TestClient(app) as c:
        yield c


def _post(client: TestClient, path: str, doc: Any = None, **body: Any) -> Any:
    if doc is not None:
        body["document"] = doc
    return client.post(path, json=body, headers=H)


def _files(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _audit() -> list[dict[str, Any]]:
    return [r for r in reversed(audit.read_recent(50)) if r["action"] == "dashboard_apply"]


# ---------------------------------------------------------------------------
# Classification and contract
# ---------------------------------------------------------------------------


def test_routes_are_classified_and_annotated(tmp_path: Path) -> None:
    assert ROUTE_SCOPES[("POST", VALIDATE)] is Scope.read  # computes and returns, writes nothing
    assert ROUTE_SCOPES[("POST", APPLY)] is Scope.operate  # changes project files, not evidence
    app = create_app(
        token=TOKEN, capsule_dir=tmp_path, db_path=tmp_path / "r.db", static_dir=None
    )
    mounted = {(m, i.path) for i in iter_routes(app) for m in i.methods}
    assert {("POST", VALIDATE), ("POST", APPLY)} <= mounted
    spec = app.openapi()
    assert spec["paths"][VALIDATE]["post"]["operationId"].startswith("dashboard")
    assert spec["paths"][APPLY]["post"]["operationId"].startswith("dashboard")


# ---------------------------------------------------------------------------
# Validate — the dry run
# ---------------------------------------------------------------------------


def test_validate_returns_a_verdict_and_writes_nothing(client: TestClient, home: Path) -> None:
    res = _post(client, VALIDATE, _widget())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["kind"] == "widget" and body["id"] == "w1"
    assert body["action"] == "create" and body["existing"] is None
    assert body["normalized"] == json.dumps(_widget(), indent=2, sort_keys=True) + "\n"
    assert body["proposed_sha256"] == hashlib.sha256(body["normalized"].encode()).hexdigest()
    assert not (home / "dashboards").exists(), "a dry run must not even create the directory"
    assert _audit() == [], "a dry run is not a mutation"


def test_validate_accepts_pasted_text_and_reports_bad_json(client: TestClient) -> None:
    ok = _post(client, VALIDATE, text=json.dumps(_widget()))
    assert ok.status_code == 200 and ok.json()["ok"] is True
    bad = _post(client, VALIDATE, text="{not json")
    assert bad.status_code == 200
    assert bad.json()["ok"] is False and "not valid JSON" in bad.json()["error"]


def test_validate_refuses_on_schema_with_the_validators_reason(client: TestClient) -> None:
    doc = _widget()
    doc["presentation"] = {"chart": "pie"}
    body = _post(client, VALIDATE, doc).json()
    assert body["ok"] is False
    assert "widget is invalid at presentation/chart" in body["error"]


def test_validate_refuses_on_the_dsl_allow_list(client: TestClient) -> None:
    body = _post(client, VALIDATE, _widget(query={"sql": "DROP TABLE runs"})).json()
    assert body["ok"] is False
    assert "DSL rejects" in body["error"]


def test_validate_shows_existing_bytes_for_an_update_and_unchanged(
    client: TestClient, root: Path
) -> None:
    assert _post(client, APPLY, _widget()).status_code == 200
    unchanged = _post(client, VALIDATE, _widget()).json()
    assert unchanged["action"] == "unchanged"
    changed = _post(client, VALIDATE, _widget(title="Renamed")).json()
    assert changed["action"] == "update"
    assert changed["existing"] == (root / "w1.widget.json").read_text()
    assert changed["current_sha256"] == hashlib.sha256(changed["existing"].encode()).hexdigest()


def test_validate_warns_about_unresolved_widget_references(client: TestClient) -> None:
    body = _post(client, VALIDATE, _dashboard(widgets=["nope"])).json()
    assert body["ok"] is True and body["kind"] == "dashboard"
    assert body["warnings"] and "nope" in body["warnings"][0]


# ---------------------------------------------------------------------------
# Apply — the write
# ---------------------------------------------------------------------------


def test_apply_writes_exactly_what_the_cli_writes(
    client: TestClient, root: Path, tmp_path: Path
) -> None:
    doc = _widget(description="x", presentation={"chart": "line", "future_field": [1, 2]})
    res = _post(client, APPLY, doc)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["changed"] is True and body["file"] == "w1.widget.json"

    cli_src = tmp_path / "w1.widget.json"
    cli_src.write_text(json.dumps(doc))
    cli_store = DashboardStore(root=tmp_path / "cli-root")
    apply_path(cli_src, cli_store)
    assert (root / "w1.widget.json").read_bytes() == (cli_store.root / "w1.widget.json").read_bytes()
    assert "future_field" in (root / "w1.widget.json").read_text(), "unknown fields survive (D6)"


def test_apply_a_dashboard_and_audit_it_without_the_body(client: TestClient, root: Path) -> None:
    _post(client, APPLY, _widget("w1"))
    res = _post(client, APPLY, _dashboard(widgets=["w1"], description="SECRET-ish text"))
    assert res.status_code == 200 and res.json()["kind"] == "dashboard"
    assert _files(root) == ["ops.dashboard.json", "w1.widget.json"]
    last = _audit()[-1]
    assert last["result"] == "ok" and last["resource"] == "dashboard:ops"
    assert last["args"]["changed"] is True and last["args"]["kind"] == "dashboard"
    assert "SECRET-ish" not in json.dumps(last)
    assert last["cli_equivalent"] == "nova dashboard show ops"


def test_apply_is_idempotent_and_does_not_churn_mtime(client: TestClient, root: Path) -> None:
    assert _post(client, APPLY, _widget()).json()["changed"] is True
    path = root / "w1.widget.json"
    os.utime(path, (1_000_000_000, 1_000_000_000))
    again = _post(client, APPLY, _widget())
    assert again.status_code == 200 and again.json()["changed"] is False
    assert path.stat().st_mtime == 1_000_000_000
    assert [r["args"]["changed"] for r in _audit()] == [True, False]


def test_apply_refusal_writes_nothing_and_is_audited_with_its_reason(
    client: TestClient, root: Path
) -> None:
    res = _post(client, APPLY, _widget(query={"sql": "DROP TABLE runs"}))
    assert res.status_code == 422
    assert "DSL rejects" in res.json()["detail"]
    assert not root.exists()
    (rec,) = _audit()
    assert rec["result"] == "refused" and "DSL rejects" in rec["error"]
    assert rec["args"]["id"] == "w1"


def test_apply_schema_refusal_is_422_and_audited(client: TestClient, root: Path) -> None:
    res = _post(client, APPLY, {"$novafabricWidget": True, "version": 1, "id": "w"})
    assert res.status_code == 422 and "invalid at" in res.json()["detail"]
    assert _files(root) == []
    assert _audit()[0]["result"] == "refused"


@pytest.mark.parametrize(
    "bad_id", ["../evil", "..", "a/b", "a\\b", "/etc/passwd", ".hidden", "UPPER", "x" * 65, ""]
)
@pytest.mark.parametrize("make", [_widget, _dashboard])
def test_ids_that_could_leave_the_directory_are_refused(
    client: TestClient, home: Path, tmp_path: Path, bad_id: str, make: Any
) -> None:
    res = _post(client, APPLY, make(bad_id))
    assert res.status_code == 422, res.text
    assert not (home / "dashboards").exists()
    for stray in (tmp_path, home, home.parent):
        assert not list(stray.glob("*evil*")) and not list(stray.glob("*.widget.json"))
    assert _audit()[0]["result"] == "refused"
    assert _post(client, VALIDATE, make(bad_id)).json()["ok"] is False


def test_oversize_body_is_413_before_parsing_and_audited(client: TestClient, root: Path) -> None:
    huge = _widget(description="x" * 300_000)
    res = _post(client, APPLY, huge)
    assert res.status_code == 413
    assert not root.exists()
    assert _audit()[0]["result"] == "refused"
    assert _post(client, VALIDATE, huge).status_code == 413


def test_a_failed_write_leaves_no_partial_or_temp_file(
    client: TestClient, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("novafabric.dashboards._store.os.replace", boom)
    res = _post(client, APPLY, _widget())
    assert res.status_code == 500 and "disk full" in res.json()["detail"]
    assert _files(root) == []
    assert _audit()[0]["result"] == "error"


def test_a_stale_preview_is_a_409_not_an_overwrite(client: TestClient, root: Path) -> None:
    _post(client, APPLY, _widget())
    preview = _post(client, VALIDATE, _widget(title="Mine")).json()
    # Someone else changes the file after the preview was taken.
    (root / "w1.widget.json").write_text(json.dumps(_widget(title="Theirs")))
    stale = _post(client, APPLY, _widget(title="Mine"), base_sha256=preview["current_sha256"])
    assert stale.status_code == 409
    assert "Theirs" in (root / "w1.widget.json").read_text()
    assert _audit()[-1]["result"] == "refused"
    fresh = _post(
        client,
        APPLY,
        _widget(title="Mine"),
        base_sha256=hashlib.sha256((root / "w1.widget.json").read_bytes()).hexdigest(),
    )
    assert fresh.status_code == 200 and fresh.json()["changed"] is True


def test_create_with_a_base_hash_of_an_absent_file_conflicts_if_it_now_exists(
    client: TestClient,
) -> None:
    _post(client, APPLY, _widget())
    res = _post(client, APPLY, _widget(title="Other"), base_sha256="")
    assert res.status_code == 409  # "" means "I expected no file"


def test_builtin_documents_are_refused(client: TestClient, root: Path) -> None:
    res = _post(client, APPLY, _dashboard(builtin=True))
    assert res.status_code == 422 and "built-in" in res.json()["detail"]
    root.mkdir(parents=True)
    (root / "ops.dashboard.json").write_text(json.dumps(_dashboard(builtin=True)))
    res = _post(client, APPLY, _dashboard())
    assert res.status_code == 422 and "built-in" in res.json()["detail"]


def test_a_symlinked_target_is_refused(client: TestClient, root: Path, tmp_path: Path) -> None:
    root.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("keep")
    (root / "w1.widget.json").symlink_to(victim)
    res = _post(client, APPLY, _widget())
    assert res.status_code == 422 and "symlink" in res.json()["detail"]
    assert victim.read_text() == "keep"


def test_a_non_object_body_is_a_422(client: TestClient) -> None:
    res = client.post(APPLY, content=b"[1,2]", headers={**H, "content-type": "application/json"})
    assert res.status_code == 422
    res = client.post(APPLY, content=b"nope", headers={**H, "content-type": "application/json"})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Scopes (real credentials, real store)
# ---------------------------------------------------------------------------


def _mint(scope: str) -> dict[str, str]:
    secret = f"issued-{scope}-token-abcdefghijklmnop"
    token_store.issue(f"test-{scope}", secret, scope)
    return {"host": "127.0.0.1:4321", "Authorization": f"Bearer {secret}"}


def test_read_scope_may_validate_but_not_apply(client: TestClient, root: Path) -> None:
    headers = _mint("read")
    ok = client.post(VALIDATE, json={"document": _widget()}, headers=headers)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    denied = client.post(APPLY, json={"document": _widget()}, headers=headers)
    assert denied.status_code == 403 and "operate" in denied.json()["detail"]
    assert not root.exists()


def test_operate_scope_may_apply(client: TestClient, root: Path) -> None:
    res = client.post(APPLY, json={"document": _widget()}, headers=_mint("operate"))
    assert res.status_code == 200
    assert (root / "w1.widget.json").is_file()


def test_unauthenticated_is_401(client: TestClient) -> None:
    res = client.post(APPLY, json={"document": _widget()}, headers={"host": "127.0.0.1:4321"})
    assert res.status_code == 401
