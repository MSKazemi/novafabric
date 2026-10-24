"""``nova serve`` sends browser security headers on every response (review frontend-dashboard-01).

Defence in depth behind the ReportChart fix: no plugins or ``<base>`` hijack, no
framing (click-jacking), no MIME sniffing, and **no Referer** — the dashboard token
travels in ``?token=``, so any outbound link would otherwise hand it to the linked
site. ``script-src`` is not restricted yet: the built dashboard has an inline
hydration script, which would need per-build hashes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import SECURITY_HEADERS, create_app  # noqa: E402

TOKEN = "test-token-security-headers-0123"
HOST = {"host": "127.0.0.1:4321"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    capsules = tmp_path / "runs"
    capsules.mkdir()
    return TestClient(create_app(token=TOKEN, capsule_dir=capsules, static_dir=None))


def _assert_headers(headers: object) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value, name  # type: ignore[index]


def test_api_responses_carry_the_security_headers(client: TestClient) -> None:
    res = client.get(f"/api/runs?token={TOKEN}", headers=HOST)
    assert res.status_code == 200
    _assert_headers(res.headers)


def test_refusals_carry_them_too(client: TestClient) -> None:
    _assert_headers(client.get("/api/runs", headers=HOST).headers)  # 401
    _assert_headers(client.get("/api/runs", headers={"host": "evil.example"}).headers)  # host guard


def test_the_policy_closes_framing_plugins_base_and_referrer() -> None:
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    for directive in ("object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'"):
        assert directive in csp
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
