"""ADR-0228 D3/D4/D5/D7 — what the scope table actually does to a request.

The table being correct (``test_authz_route_table.py``) and the table being
*enforced* are two different claims, and this repo has shipped a pair of
self-consistent artifacts with nothing spanning them before. These tests span
them: they drive real HTTP requests with real credentials.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import audit, token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.authz import Scope, build_authz_dependency, resolve_scope  # noqa: E402

SERVER_TOKEN = "server-token-0123456789abcdef"
HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate ~/.novafabric so tokens.jsonl and the audit log are this test's own."""
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
    app = create_app(token=SERVER_TOKEN, capsule_dir=capsules, static_mounted_by_caller=True)
    with TestClient(app) as c:
        yield c


def _mint(scope: str) -> str:
    """Issue a real credential at *scope* through the real store."""
    secret = f"issued-{scope}-token-abcdefghijklmnop"
    token_store.issue(f"test-{scope}", secret, scope)
    return secret


def _audit_records() -> list[dict[str, object]]:
    path = Path(audit.__dict__["_path"]())
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# D4 — the default local posture is byte-identical
# ---------------------------------------------------------------------------


def test_server_token_still_reaches_every_tier(client: TestClient) -> None:
    """The .serve-token holds admin, so a laptop sees no change at all."""
    for path in ("/api/runs", "/api/audit", "/api/admin/roles", "/api/stats"):
        r = client.get(f"{path}?token={SERVER_TOKEN}", headers=HEADERS)
        assert r.status_code != 403, f"{path} -> {r.status_code} {r.text[:200]}"


def test_public_routes_need_no_credential(client: TestClient) -> None:
    for path in ("/api/health", "/livez", "/readyz", "/api/openapi.json"):
        assert client.get(path, headers=HEADERS).status_code == 200, path


def test_legacy_issued_record_without_scope_is_admin(client: TestClient, home: Path) -> None:
    """A record written before ADR-0228 has no scope key and must keep its access.

    Narrowing it retroactively would silently revoke access an operator was
    granted — the opposite of a safe default in this direction.
    """
    secret = "legacy-token-zyxwvutsrqponmlk"
    legacy = {
        "label": "pre-adr-0228",
        "fingerprint": token_store.fingerprint(secret),
        "token_digest": token_store.digest(secret),
        "created_at": "2026-08-01T00:00:00+00:00",
        "revoked": False,
    }
    token_store.tokens_path().write_text(json.dumps(legacy) + "\n")

    assert "scope" not in legacy
    assert resolve_scope(secret, server_token=SERVER_TOKEN) == (legacy["fingerprint"], Scope.admin)
    r = client.get(f"/api/admin/roles?token={secret}", headers=HEADERS)
    assert r.status_code != 403


# ---------------------------------------------------------------------------
# D5 + the E2 escalation bridge
# ---------------------------------------------------------------------------


def test_read_token_is_refused_the_role_assignment_bridge(client: TestClient) -> None:
    """E2 closed: minting a server-mode role is now an admin-scope action."""
    secret = _mint("read")
    r = client.post(
        f"/api/admin/roles?token={secret}",
        headers=HEADERS,
        json={"subject": "mallory", "role": "admin"},
    )
    assert r.status_code == 403
    assert "admin" in r.json()["detail"]


def test_read_token_can_still_read(client: TestClient) -> None:
    secret = _mint("read")
    assert client.get(f"/api/runs?token={secret}", headers=HEADERS).status_code == 200
    assert client.get(f"/api/stats?token={secret}", headers=HEADERS).status_code == 200


def test_read_token_cannot_destroy_evidence(client: TestClient) -> None:
    secret = _mint("read")
    r = client.delete(f"/api/runs/01TEST00000000000000000001?token={secret}", headers=HEADERS)
    assert r.status_code == 403


def test_read_token_cannot_reach_the_audit_trail(client: TestClient) -> None:
    """D1a: the trail says who did what, so an ordinary reader does not get it."""
    secret = _mint("read")
    assert client.get(f"/api/audit?token={secret}", headers=HEADERS).status_code == 403


def test_audit_token_reaches_the_trail_and_reads_but_never_mutates(client: TestClient) -> None:
    secret = _mint("audit")
    assert client.get(f"/api/audit?token={secret}", headers=HEADERS).status_code == 200
    assert client.get(f"/api/runs?token={secret}", headers=HEADERS).status_code == 200
    assert client.post(
        f"/api/holds?token={secret}", headers=HEADERS, json={}
    ).status_code == 403
    assert client.post(
        f"/api/admin/roles?token={secret}",
        headers=HEADERS,
        json={"subject": "x", "role": "admin"},
    ).status_code == 403


def test_operate_token_mutates_but_is_refused_admin(client: TestClient) -> None:
    secret = _mint("operate")
    assert client.get(f"/api/runs?token={secret}", headers=HEADERS).status_code == 200
    r = client.post(
        f"/api/compliance/pii/erase?token={secret}", headers=HEADERS, json={}
    )
    assert r.status_code == 403


def test_bearer_header_is_enforced_too(client: TestClient) -> None:
    """The SPA sends a Bearer header; the ?token= form is the legacy path."""
    secret = _mint("read")
    r = client.post(
        "/api/admin/roles",
        headers={**HEADERS, "Authorization": f"Bearer {secret}"},
        json={"subject": "mallory", "role": "admin"},
    )
    assert r.status_code == 403


def test_issue_rejects_an_unknown_scope() -> None:
    with pytest.raises(ValueError, match="unknown scope"):
        token_store.issue("bad", "some-secret-value-here-0123", "superuser")


def test_issue_rejects_public_as_a_grantable_scope() -> None:
    with pytest.raises(ValueError, match="not a grantable scope"):
        token_store.issue("bad", "another-secret-value-4567", "public")


def test_minting_endpoint_round_trips_the_scope(client: TestClient) -> None:
    r = client.post(
        f"/api/admin/tokens?token={SERVER_TOKEN}",
        headers=HEADERS,
        json={"label": "auditor-laptop", "confirmed": True, "scope": "audit"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "audit"
    # What the endpoint says is what the enforcement layer reads back.
    assert resolve_scope(body["token"], server_token=SERVER_TOKEN)[1] is Scope.audit
    listed = client.get(f"/api/admin/tokens?token={SERVER_TOKEN}", headers=HEADERS).json()
    assert listed["tokens"][0]["scope"] == "audit"


def test_minting_endpoint_rejects_a_bad_scope(client: TestClient) -> None:
    r = client.post(
        f"/api/admin/tokens?token={SERVER_TOKEN}",
        headers=HEADERS,
        json={"label": "x", "confirmed": True, "scope": "root"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# AC6 — 401 still wins over 403
# ---------------------------------------------------------------------------


def test_missing_credential_is_401_not_403(client: TestClient) -> None:
    """Answering 403 to an unauthenticated request answers the wrong question."""
    assert client.get("/api/runs", headers=HEADERS).status_code == 401
    assert client.post(
        "/api/admin/roles", headers=HEADERS, json={"subject": "x", "role": "admin"}
    ).status_code == 401


def test_unknown_credential_is_401_not_403(client: TestClient) -> None:
    assert client.get("/api/runs?token=not-a-real-token", headers=HEADERS).status_code == 401


# ---------------------------------------------------------------------------
# D3 — fail closed, proved rather than asserted
# ---------------------------------------------------------------------------


def test_an_unclassified_route_is_denied(tmp_path: Path, home: Path) -> None:
    """Red-green for the branch the completeness guard exists to keep unreachable.

    Mounting a route that is deliberately absent from ROUTE_SCOPES is the only
    way to exercise D3; asserting on ``required_scope`` alone would prove the
    lookup, not the denial.
    """
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(token=SERVER_TOKEN, capsule_dir=capsules, static_mounted_by_caller=True)

    @app.get("/api/route-added-after-the-fact")
    async def rogue() -> dict[str, str]:  # pragma: no cover - must never execute
        return {"leaked": "everything"}

    with TestClient(app) as c:
        r = c.get(f"/api/route-added-after-the-fact?token={SERVER_TOKEN}", headers=HEADERS)
    assert r.status_code == 403, "an unclassified route must deny, not default to read"
    assert "no authorization classification" in r.json()["detail"]


def test_fail_closed_denies_even_the_admin_server_token(tmp_path: Path, home: Path) -> None:
    """D3 is not "deny the weak" — an unclassified route denies everyone."""
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(token=SERVER_TOKEN, capsule_dir=capsules, static_mounted_by_caller=True)

    @app.post("/api/another-unclassified")
    async def rogue() -> dict[str, str]:  # pragma: no cover - must never execute
        return {"ok": "no"}

    with TestClient(app) as c:
        r = c.post(f"/api/another-unclassified?token={SERVER_TOKEN}", headers=HEADERS)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# D7 — the 403 is audited, the 401 is not, and neither leaks the credential
# ---------------------------------------------------------------------------


def test_denial_is_audited_with_required_and_held_scope(client: TestClient) -> None:
    secret = _mint("read")
    client.post(
        f"/api/admin/roles?token={secret}",
        headers=HEADERS,
        json={"subject": "mallory", "role": "admin"},
    )
    denials = [r for r in _audit_records() if r.get("action") == "authz.denied"]
    assert len(denials) == 1
    record = denials[0]
    assert record["args"] == {"method": "POST", "route": "/api/admin/roles"}
    # ADR-0231 D4 promoted the scopes to top-level fields. A SIEM rule that has
    # to reach into a free-form payload to find them is a rule that breaks the
    # next time that payload's shape changes.
    assert record["required_scope"] == "admin"
    assert record["held_scope"] == "read"
    assert record["resource"] == "POST /api/admin/roles"
    assert record["result"] == "denied"
    assert record["actor_token_fp"] == token_store.fingerprint(secret)
    # An issued token is a deliberately minted per-holder credential, so it is
    # honestly stronger evidence than the one shared browser token.
    assert record["actor"]["identity_source"] == "credential"


def test_a_401_writes_no_audit_record(client: TestClient) -> None:
    """v0.98.0 took the bearer token off the logger; a 401 must not put it back."""
    client.get("/api/runs?token=stale-browser-tab-token", headers=HEADERS)
    assert [r for r in _audit_records() if r.get("action") == "authz.denied"] == []


def test_no_audit_record_contains_the_presented_secret(client: TestClient) -> None:
    secret = _mint("read")
    client.get(f"/api/audit?token={secret}", headers=HEADERS)
    blob = json.dumps(_audit_records())
    assert secret not in blob
    assert token_store.fingerprint(secret) in blob


def test_a_failing_audit_write_still_denies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit failure must never be a way to get in."""
    secret = _mint("read")

    def boom(**_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(audit, "append", boom)
    r = client.get(f"/api/audit?token={secret}", headers=HEADERS)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# The dependency in isolation
# ---------------------------------------------------------------------------


def test_resolve_scope_returns_none_for_an_unknown_credential(home: Path) -> None:
    assert resolve_scope("nobody-knows-this", server_token=SERVER_TOKEN) is None


def test_resolve_scope_maps_the_server_token_to_local_admin(home: Path) -> None:
    assert resolve_scope(SERVER_TOKEN, server_token=SERVER_TOKEN) == ("local", Scope.admin)


def test_build_authz_dependency_asks_for_the_connection_not_the_request() -> None:
    """Two failure modes are pinned here, and both were live during development.

    The PEP 563 workaround (binding the real class into ``__annotations__``)
    must survive refactors, or FastAPI cannot resolve the lazily-imported name
    and demands it as a query parameter — every route then answers 422.

    And it must be ``HTTPConnection``, never ``Request``: an app-level
    dependency is attached to WebSocket routes too, and FastAPI hands those a
    ``WebSocket``, so a ``Request`` annotation raises a missing-argument
    TypeError at connect time and takes ``/topology/stream`` and
    ``/api/tv5/ws`` down with it.
    """
    from starlette.requests import HTTPConnection

    dep = build_authz_dependency(SERVER_TOKEN)
    assert callable(dep)
    assert dep.__annotations__["connection"] is HTTPConnection


def test_websockets_are_left_to_their_own_inline_guard(tmp_path: Path, home: Path) -> None:
    """ADR-0228 OQ-2 is deferred, so a WS connection must not meet this dependency.

    Asserted as behaviour rather than as an annotation, because the regression
    that motivated it broke the *handshake*: an app-level dependency annotated
    ``Request`` raises a missing-argument ``TypeError`` on a WebSocket route,
    which surfaces as an opaque 1011 internal-error close. Reaching the inline
    guard's own **4401** proves the dependency stood aside and the handler ran.
    """
    pytest.importorskip("duckdb")
    from starlette.websockets import WebSocketDisconnect

    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(
        token=SERVER_TOKEN,
        capsule_dir=capsules,
        static_mounted_by_caller=True,
        topology_enabled=True,
    )
    with TestClient(app) as c, pytest.raises(WebSocketDisconnect) as excinfo:
        # The TDP v1 subprotocol has to be offered or the handler refuses with
        # 4400 before it ever reaches the token check.
        with c.websocket_connect(
            "/topology/stream",
            headers={"sec-websocket-protocol": "nova-tdp-v1", "host": "127.0.0.1:4321"},
        ):
            pass  # pragma: no cover - the connect must be refused
    assert excinfo.value.code == 4401, (
        f"expected the inline token guard (4401); got {excinfo.value.code} — "
        "1011 means the authz dependency crashed the handshake"
    )
