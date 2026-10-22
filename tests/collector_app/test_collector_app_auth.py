"""The cluster collector authenticates callers and takes the tenant from that identity.

Three defects, each with an attack test:

(a) With ``NOVA_COLLECTOR_TOKEN`` unset, auth was skipped for **every** caller, so a
    collector started on ``0.0.0.0`` accepted evidence from anyone who could reach it.
    Unset now means local mode: only a loopback peer, addressing a localhost ``Host``,
    not via a proxy, is served — and a loud warning is logged at startup.
(b) The token was compared with ``!=``, whose run time depends on the matching prefix.
(c) The tenant was read from the request body (and from each event's own ``tenant``
    key), so any caller could write evidence into any tenant. The tenant now comes
    from the authenticated principal (``NOVA_COLLECTOR_TENANT``); a body or event
    tenant that disagrees is refused, not rewritten.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from novafabric.collector_app.app import create_collector_app

TOKEN = "collector-token-0123456789"
LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("10.1.2.3", 50000)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_COLLECTOR_TOKEN", raising=False)
    monkeypatch.delenv("NOVA_COLLECTOR_TENANT", raising=False)


def _acc() -> Any:
    acc = MagicMock(spec=["ingest_capsule_events"])
    acc.ingest_capsule_events = MagicMock(return_value=1)
    return acc


def _client(
    acc: Any, *, peer: tuple[str, int] = LOOPBACK, base_url: str = "http://127.0.0.1:8090"
) -> TestClient:
    return TestClient(create_collector_app(accumulator=acc), client=peer, base_url=base_url)


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"capsule_id": "cap-1", "events": [{"type": "llm_call"}]}
    body.update(overrides)
    return body


# ── (a) no token configured: local mode, loopback only ───────────────────────


@pytest.mark.parametrize(
    ("peer", "base_url"),
    [(LOOPBACK, "http://127.0.0.1:8090"), (("::1", 50000), "http://[::1]:8090"),
     (LOOPBACK, "http://localhost:8090")],
)
def test_without_a_token_a_loopback_caller_is_served(
    peer: tuple[str, int], base_url: str
) -> None:
    acc = _acc()
    resp = _client(acc, peer=peer, base_url=base_url).post("/capsule", json=_payload())
    assert resp.status_code == 200, resp.text
    acc.ingest_capsule_events.assert_called_once()


def test_without_a_token_a_remote_caller_is_refused() -> None:
    """The attack: the collector was started on 0.0.0.0 and nobody set a token."""
    acc = _acc()
    resp = _client(acc, peer=REMOTE).post("/capsule", json=_payload())
    assert resp.status_code == 403
    assert "NOVA_COLLECTOR_TOKEN" in resp.json()["detail"]
    acc.ingest_capsule_events.assert_not_called()


def test_without_a_token_a_non_localhost_host_header_is_refused() -> None:
    """DNS rebinding: a browser on this machine sends a loopback peer, a foreign Host."""
    acc = _acc()
    resp = _client(acc, base_url="http://attacker.example:8090").post(
        "/capsule", json=_payload()
    )
    assert resp.status_code == 403
    acc.ingest_capsule_events.assert_not_called()


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP"])
def test_without_a_token_a_proxied_request_is_refused(header: str) -> None:
    """A reverse proxy on this host makes every remote caller look like loopback."""
    acc = _acc()
    resp = _client(acc).post("/capsule", json=_payload(), headers={header: "203.0.113.9"})
    assert resp.status_code == 403
    acc.ingest_capsule_events.assert_not_called()


def test_without_a_peer_address_the_caller_is_refused() -> None:
    """A transport that reports no peer cannot be shown to be loopback: fail closed."""
    from novafabric.collector_app.app import _is_local_request

    request = MagicMock()
    request.client = None
    request.headers = {"host": "127.0.0.1"}
    assert _is_local_request(request) is False


def test_without_a_token_startup_warns_loudly(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="novafabric.collector_app.app"):
        create_collector_app(accumulator=_acc())
    assert any(
        "NOVA_COLLECTOR_TOKEN" in r.getMessage() and r.levelno >= logging.WARNING
        for r in caplog.records
    ), "an unauthenticated collector must say so at startup"


def test_with_a_token_startup_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", TOKEN)
    with caplog.at_level(logging.WARNING, logger="novafabric.collector_app.app"):
        create_collector_app(accumulator=_acc())
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_health_stays_open_for_liveness_probes() -> None:
    assert _client(_acc(), peer=REMOTE).get("/health").status_code == 200


# ── token configured: required from everyone, compared in constant time ─────


def test_with_a_token_a_remote_caller_with_the_token_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", TOKEN)
    acc = _acc()
    resp = _client(acc, peer=REMOTE, base_url="http://collector.cluster:8090").post(
        "/capsule", json=_payload(), headers={"X-Nova-Token": TOKEN}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("presented", [None, "", "wrong", TOKEN[:-1], TOKEN + "x"])
def test_with_a_token_loopback_is_not_an_exemption(
    monkeypatch: pytest.MonkeyPatch, presented: str | None
) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", TOKEN)
    acc = _acc()
    headers = {} if presented is None else {"X-Nova-Token": presented}
    resp = _client(acc).post("/capsule", json=_payload(), headers=headers)
    assert resp.status_code == 401
    acc.ingest_capsule_events.assert_not_called()


def test_the_token_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", TOKEN)
    calls: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    client = _client(_acc())
    assert client.post("/capsule", json=_payload(),
                       headers={"X-Nova-Token": "wrong"}).status_code == 401
    assert client.post("/capsule", json=_payload(),
                       headers={"X-Nova-Token": TOKEN}).status_code == 200
    assert len(calls) == 2, "every presented token must go through hmac.compare_digest"


# ── (c) the tenant is the authenticated principal's, never the caller's claim ──


def _ingested_tenants(acc: Any) -> set[str]:
    return {e["tenant"] for e in acc.ingest_capsule_events.call_args[0][0]}


def test_the_tenant_comes_from_the_principal_when_the_body_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", TOKEN)
    monkeypatch.setenv("NOVA_COLLECTOR_TENANT", "acme")
    acc = _acc()
    resp = _client(acc).post("/capsule", json=_payload(), headers={"X-Nova-Token": TOKEN})
    assert resp.status_code == 200
    assert _ingested_tenants(acc) == {"acme"}


def test_a_matching_body_tenant_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", TOKEN)
    monkeypatch.setenv("NOVA_COLLECTOR_TENANT", "acme")
    acc = _acc()
    resp = _client(acc).post(
        "/capsule", json=_payload(tenant_id="acme"), headers={"X-Nova-Token": TOKEN}
    )
    assert resp.status_code == 200
    assert _ingested_tenants(acc) == {"acme"}


def test_a_body_tenant_other_than_the_principals_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attack: a valid caller writes evidence into somebody else's tenant."""
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", TOKEN)
    monkeypatch.setenv("NOVA_COLLECTOR_TENANT", "acme")
    acc = _acc()
    resp = _client(acc).post(
        "/capsule", json=_payload(tenant_id="victim"), headers={"X-Nova-Token": TOKEN}
    )
    assert resp.status_code == 403
    acc.ingest_capsule_events.assert_not_called()


def test_an_event_tenant_other_than_the_principals_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-event ``tenant`` used to win over everything (``setdefault``)."""
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", TOKEN)
    monkeypatch.setenv("NOVA_COLLECTOR_TENANT", "acme")
    acc = _acc()
    body = _payload(tenant_id="acme", events=[{"type": "x"}, {"type": "y", "tenant": "victim"}])
    resp = _client(acc).post("/capsule", json=body, headers={"X-Nova-Token": TOKEN})
    assert resp.status_code == 403
    acc.ingest_capsule_events.assert_not_called()


def test_local_mode_principal_is_the_default_tenant() -> None:
    acc = _acc()
    assert _client(acc).post("/capsule", json=_payload()).status_code == 200
    assert _ingested_tenants(acc) == {"default"}
    assert _client(_acc()).post(
        "/capsule", json=_payload(tenant_id="other")
    ).status_code == 403
