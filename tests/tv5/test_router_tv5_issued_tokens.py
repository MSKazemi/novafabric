"""Audit L26 — the TV-5 HTTP routes honour issued tokens by scope.

``ROUTE_SCOPES`` classifies ``/api/tv5/{live,windows,snapshot/{window_id}}`` as
``read``, and the app-level scope dependency let a minted read token through —
but the router's own ``verify`` compared the credential only against the session
token, so the same token then got 401. Every other route authenticates issued
tokens (ADR-0252) and lets the scope table decide (ADR-0228).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D  # noqa: E402
from novafabric.serve.topology.router_tv5 import make_tv5_router  # noqa: E402
from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D  # noqa: E402

SERVER_TOKEN = "server-token-0123456789abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TV5_GETS = ("/api/tv5/live", "/api/tv5/windows")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    h = tmp_path / "home"
    (h / ".novafabric").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h / ".novafabric"))
    monkeypatch.setenv(
        "NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(h / ".novafabric" / "audit.jsonl")
    )
    yield h


def _router(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = SnapshotStore3D(tmp_path / "snapshots")
    return make_tv5_router(
        store, LayoutPipeline3D(store), token=SERVER_TOKEN, start_retention_loop=False
    )


@pytest.fixture
def client(tmp_path: Path, home: Path) -> Iterator[TestClient]:
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(
        token=SERVER_TOKEN,
        capsule_dir=capsules,
        static_mounted_by_caller=True,
        extra_routers=[_router(tmp_path)],
    )
    with TestClient(app) as c:
        yield c


def _mint(scope: str) -> str:
    secret = f"issued-{scope}-token-abcdefghijklmnop"
    token_store.issue(f"test-{scope}", secret, scope)
    return secret


@pytest.mark.parametrize("scope", ["read", "audit", "operate", "admin"])
def test_issued_token_reaches_tv5_reads(client: TestClient, scope: str) -> None:
    secret = _mint(scope)
    for path in TV5_GETS:
        resp = client.get(path, headers={**HEADERS, "Authorization": f"Bearer {secret}"})
        assert resp.status_code == 200, (path, resp.status_code, resp.text)
    resp = client.get(f"/api/tv5/live?token={secret}", headers=HEADERS)
    assert resp.status_code == 200


def test_server_token_still_works(client: TestClient) -> None:
    for path in TV5_GETS:
        resp = client.get(path, headers={**HEADERS, "Authorization": f"Bearer {SERVER_TOKEN}"})
        assert resp.status_code == 200


def test_revoked_issued_token_is_401(client: TestClient) -> None:
    secret = _mint("read")
    assert token_store.revoke(token_store.fingerprint(secret))
    resp = client.get("/api/tv5/live", headers={**HEADERS, "Authorization": f"Bearer {secret}"})
    assert resp.status_code == 401


def test_unknown_token_is_401(client: TestClient) -> None:
    resp = client.get("/api/tv5/live", headers={**HEADERS, "Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_standalone_router_also_honours_issued_tokens(tmp_path: Path, home: Path) -> None:
    """Mounted on a bare app (no scope dependency), the router still authenticates."""
    app = FastAPI()
    app.include_router(_router(tmp_path))
    c = TestClient(app)
    secret = _mint("read")
    assert c.get(f"/api/tv5/live?token={secret}").status_code == 200
    assert c.get("/api/tv5/live?token=wrong").status_code == 401


def test_standalone_router_refuses_a_scopeless_record(tmp_path: Path, home: Path) -> None:
    """A record with an unrecognised scope authenticates but holds no scope: 403."""
    app = FastAPI()
    app.include_router(_router(tmp_path))
    c = TestClient(app)
    secret = "issued-weird-token-abcdefghijklmnop"
    token_store.issue("weird", secret, "read")
    records = token_store.load()
    for rec in records:
        if rec.get("label") == "weird":
            rec["scope"] = "nonsense"
    token_store.__dict__["_write_all"](token_store.tokens_path(), records)
    assert c.get(f"/api/tv5/live?token={secret}").status_code == 403
