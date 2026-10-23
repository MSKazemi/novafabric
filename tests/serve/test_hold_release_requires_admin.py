"""Releasing a legal hold needs `admin` (ADR-0031 §3); placing one stays `operate`.

ADR-0228's scope table filed hold release with the "bounded, reversible"
`operate` mutations. Release is not reversible once a delete follows it: with
`operate` able to release, the only thing between an `operate` credential and
the destruction of held evidence was the `admin` delete. ADR-0031 — the
decision that owns legal holds — requires admin for release, so the route
table now says so. Placing a hold only ever protects more data, so it stays
`operate`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.authz import ROUTE_SCOPES, Scope  # noqa: E402

SERVER_TOKEN = "server-token-0123456789abcdef"
HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    home = tmp_path / "home" / ".novafabric"
    home.mkdir(parents=True)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "audit.jsonl"))
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(token=SERVER_TOKEN, capsule_dir=capsules, static_mounted_by_caller=True)
    with TestClient(app) as c:
        yield c


def _mint(scope: str) -> str:
    secret = f"issued-{scope}-token-abcdefghijklmnop"
    token_store.issue(f"test-{scope}", secret, scope)
    return secret


def _place(client: TestClient, token: str) -> str:
    r = client.post(
        "/api/holds",
        params={"token": token},
        headers=HEADERS,
        json={"registry": "reg1", "reason": "litigation"},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["hold_id"])


def test_route_table_files_release_under_admin_and_placement_under_operate() -> None:
    assert ROUTE_SCOPES[("POST", "/api/holds/{hold_id}/release")] is Scope.admin
    assert ROUTE_SCOPES[("POST", "/api/holds")] is Scope.operate


def test_operate_can_place_a_hold_but_not_release_it(client: TestClient) -> None:
    operate = _mint("operate")
    hold_id = _place(client, operate)

    r = client.post(f"/api/holds/{hold_id}/release", params={"token": operate}, headers=HEADERS)
    assert r.status_code == 403, r.text

    listed = client.get("/api/holds", params={"token": SERVER_TOKEN}, headers=HEADERS).json()
    assert listed["total_active"] == 1, "a refused release must leave the hold active"


def test_admin_can_release_a_hold(client: TestClient) -> None:
    hold_id = _place(client, _mint("operate"))
    r = client.post(
        f"/api/holds/{hold_id}/release", params={"token": _mint("admin")}, headers=HEADERS
    )
    assert r.status_code == 200, r.text
    assert r.json()["released"] is True
