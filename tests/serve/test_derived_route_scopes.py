"""serve-02 / serve-03: a derived route is never weaker than the route it derives from.

serve-02: ``GET /api/reports/{report_id}/export`` was one ``read`` row in the
scope table, but it renders *any* report. A read token got 403 on
``/api/reports/dashboard-audit`` and 200 on ``/api/reports/dashboard-audit/export``
with the same rows (the dashboard audit log, the policy audit, the API-key
inventory) as HTML.

serve-03: ``GET /api/policy/explain?decision_id=`` matched any entry whose
decision id *started with* the query, so an empty id returned the whole
dashboard audit log — to a read token, on a route classified ``read``.

Both were classification defects, not enforcement gaps: the table said the
wrong thing. So the guards here are about the table as a whole (every derived
route, every report), driven through real HTTP where it matters.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.authz import (  # noqa: E402
    DERIVED_ROUTE_BASES,
    ROUTE_SCOPES,
    Scope,
    at_least,
    effective_scope,
    grantable_scopes,
    satisfies,
)
from novafabric.serve.introspect import iter_routes  # noqa: E402
from novafabric.serve.report_registry import REPORTS  # noqa: E402

SERVER_TOKEN = "server-token-0123456789abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
_EXPORT = ("GET", "/api/reports/{report_id}/export")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    h = tmp_path / "home"
    (h / ".novafabric").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h / ".novafabric"))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(h / ".novafabric" / "audit.jsonl"))
    yield h


@pytest.fixture
def client(tmp_path: Path, home: Path) -> Iterator[TestClient]:
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(
        token=SERVER_TOKEN,
        capsule_dir=capsules,
        db_path=tmp_path / "registry.db",
        static_mounted_by_caller=True,
    )
    with TestClient(app) as c:
        yield c


def _mint(scope: str) -> str:
    secret = f"issued-{scope}-token-abcdefghijklmnop"
    token_store.issue(f"test-{scope}", secret, scope)
    return secret


def _get(client: TestClient, path: str, token: str, **params: str) -> int:
    return client.get(
        path, params=params, headers={**HEADERS, "Authorization": f"Bearer {token}"}
    ).status_code


# ---------------------------------------------------------------------------
# serve-02 through HTTP: every report, every scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["read", "operate", "audit", "admin"])
def test_report_export_is_refused_exactly_where_its_base_report_is(
    client: TestClient, scope: str
) -> None:
    token = _mint(scope)
    mismatched = []
    for rid in sorted(REPORTS):
        base_denied = _get(client, f"/api/reports/{rid}", token) == 403
        export_denied = _get(client, f"/api/reports/{rid}/export", token) == 403
        if base_denied != export_denied:
            mismatched.append((rid, f"base 403={base_denied}", f"export 403={export_denied}"))
    assert not mismatched, f"{scope} token: export scope differs from its base: {mismatched}"


@pytest.mark.parametrize(
    ("rid", "denied_to"),
    [
        ("dashboard-audit", {"read", "operate"}),
        ("policy-audit", {"read", "operate"}),
        ("api-key-inventory", {"read", "operate", "audit"}),
    ],
)
def test_the_reported_bypass_is_closed(
    client: TestClient, rid: str, denied_to: set[str]
) -> None:
    """The three reports the reviewer exported with a read token."""
    for scope in ("read", "operate", "audit", "admin"):
        status = _get(client, f"/api/reports/{rid}/export", _mint(scope))
        if scope in denied_to:
            assert status == 403, f"{scope} exported {rid}: {status}"
        else:
            assert status != 403, f"{scope} was refused {rid}"


def test_an_unknown_report_export_needs_admin_even_to_learn_it_is_unknown(
    client: TestClient,
) -> None:
    assert _get(client, "/api/reports/no-such-report/export", _mint("read")) == 403
    assert _get(client, "/api/reports/no-such-report/export", SERVER_TOKEN) == 404


# ---------------------------------------------------------------------------
# serve-02 over the table: a derived route is at least its base
# ---------------------------------------------------------------------------

_DERIVED_SUFFIX = re.compile(r"(/export|/download|/csv|/pdf|/html|/json|\.csv|\.json|\.pdf|\.html)$")


def _mounted(tmp_path: Path) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for topology in (False, True):
        app = create_app(token="x" * 32, capsule_dir=tmp_path, topology_enabled=topology)
        for route in iter_routes(app):
            pairs.update((m, route.path) for m in route.methods if m not in ("HEAD", "OPTIONS"))
    return pairs


def test_every_derived_route_is_at_least_as_strong_as_its_base(tmp_path: Path) -> None:
    """Any ``X/export``, ``X/download``, ``X.csv``… needs at least ``X``'s scope."""
    mounted = _mounted(tmp_path)
    weaker = []
    checked = 0
    for method, path in sorted(mounted):
        if not _DERIVED_SUFFIX.search(path) or ROUTE_SCOPES.get((method, path)) is Scope.public:
            continue
        base = _DERIVED_SUFFIX.sub("", path)
        if ("GET", base) not in mounted:
            continue
        checked += 1
        if not at_least(ROUTE_SCOPES[(method, path)], ROUTE_SCOPES[("GET", base)]):
            weaker.append((method, path, ROUTE_SCOPES[(method, path)], base))
    assert checked >= 3, "the derived-route sweep found nothing to check; is the regex stale?"
    assert not weaker, f"derived routes weaker than their base: {weaker}"


def test_every_report_has_a_classified_base_and_its_export_inherits_it() -> None:
    """A report added without a base row would otherwise export at the template's floor."""
    method, template = _EXPORT
    for rid in REPORTS:
        base = ROUTE_SCOPES.get(("GET", f"/api/reports/{rid}"))
        assert base is not None, f"report {rid!r} has no classified base route"
        assert effective_scope(method, template, {"report_id": rid}) is base, rid


def test_every_derived_template_has_a_table_row() -> None:
    for key in DERIVED_ROUTE_BASES:
        assert key in ROUTE_SCOPES, key


@pytest.mark.parametrize("value", ["", "a/b", None, 7])
def test_a_malformed_derived_parameter_needs_admin(value: object) -> None:
    method, template = _EXPORT
    assert effective_scope(method, template, {"report_id": value}) is Scope.admin
    assert effective_scope(method, template, {}) is Scope.admin


def test_routes_without_a_derivation_rule_are_unchanged() -> None:
    for (method, path), scope in ROUTE_SCOPES.items():
        if (method, path) in DERIVED_ROUTE_BASES:
            continue
        assert effective_scope(method, path, {"report_id": "dashboard-audit"}) is scope


def test_at_least_is_the_lattice_order() -> None:
    for a in grantable_scopes():
        for b in grantable_scopes():
            expected = all(satisfies(h, b) for h in grantable_scopes() if satisfies(h, a))
            assert at_least(a, b) is expected
    assert at_least(Scope.audit, Scope.read)
    assert not at_least(Scope.read, Scope.audit)
    assert not at_least(Scope.operate, Scope.audit)
    assert not at_least(Scope.audit, Scope.operate)
    assert at_least(Scope.admin, Scope.audit)


# ---------------------------------------------------------------------------
# serve-03: /api/policy/explain
# ---------------------------------------------------------------------------


def _seed_audit(home: Path) -> None:
    path = home / ".novafabric" / "audit.jsonl"
    rows = [
        {"audit_id": "aud-0001", "action": "policy_eval", "args": {"decision_id": "dec-abc-123"}},
        {"audit_id": "aud-0002", "action": "issue_token", "args": {"label": "ops"}},
        {"audit_id": "aud-0003", "action": "authz.denied", "args": {"route": "/api/audit"}},
        {"audit_id": "aud-0004", "action": "policy_eval", "extra": {"decision_id": "dec-abc-456"}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _explain(client: TestClient, token: str, decision_id: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    r = client.get(
        "/api/policy/explain",
        params={"decision_id": decision_id},
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})


@pytest.mark.parametrize("scope", ["read", "operate"])
def test_explain_is_refused_to_scopes_that_cannot_read_the_audit_trail(
    client: TestClient, home: Path, scope: str
) -> None:
    _seed_audit(home)
    for did in ("dec-abc-123", "", "dec"):
        status, body = _explain(client, _mint(scope), did)
        assert status == 403, (did, status)
        assert "aud-0002" not in json.dumps(body)


@pytest.mark.parametrize("did", ["dec", "dec-abc", "dec-abc-12", "aud-000"])
def test_a_partial_decision_id_returns_nothing(client: TestClient, home: Path, did: str) -> None:
    _seed_audit(home)
    for token in (_mint("audit"), SERVER_TOKEN):
        status, body = _explain(client, token, did)
        assert status == 200, (did, status)
        assert body["entries"] == [] and body["count"] == 0, (did, body)


@pytest.mark.parametrize("did", ["", " ", "*", ".*", "%", "dec*", "-x", "a" * 129])
def test_an_empty_or_wildcard_decision_id_is_rejected(
    client: TestClient, home: Path, did: str
) -> None:
    """Not a lookup at all: 422 before the audit log is opened."""
    _seed_audit(home)
    for token in (_mint("audit"), SERVER_TOKEN):
        status, body = _explain(client, token, did)
        assert status == 422, (did, status)
        assert "aud-0002" not in json.dumps(body)


@pytest.mark.parametrize(
    ("did", "audit_id"),
    [("dec-abc-123", "aud-0001"), ("dec-abc-456", "aud-0004"), ("aud-0002", "aud-0002")],
)
def test_an_exact_id_returns_only_that_entry(
    client: TestClient, home: Path, did: str, audit_id: str
) -> None:
    _seed_audit(home)
    status, body = _explain(client, _mint("audit"), did)
    assert status == 200
    assert [e["audit_id"] for e in body["entries"]] == [audit_id]
