"""S6 scale slice — conditional-GET (ETag) + Cache-Control (ADR-0199).

Unit-tests the ``serve.http_cache`` helper and pins the end-to-end conditional
behaviour on a real read endpoint (``/api/analytics/summary``): a first GET
returns 200 + an ETag + Cache-Control; a follow-up GET echoing that ETag in
``If-None-Match`` gets a 304 with no body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.http_cache import (  # noqa: E402
    compute_etag,
    conditional_json,
)

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


def _request(if_none_match: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if if_none_match is not None:
        headers.append((b"if-none-match", if_none_match.encode()))
    return Request({"type": "http", "headers": headers, "method": "GET"})


class TestHelper:
    def test_etag_is_content_addressed(self) -> None:
        a = compute_etag({"x": 1, "y": 2})
        b = compute_etag({"y": 2, "x": 1})  # key order must not matter
        c = compute_etag({"x": 1, "y": 3})
        assert a == b
        assert a != c
        assert a.startswith('"') and a.endswith('"')

    def test_returns_200_with_headers_when_no_match(self) -> None:
        resp = conditional_json(_request(), {"a": 1}, max_age=30)
        assert resp.status_code == 200
        assert resp.headers["ETag"] == compute_etag({"a": 1})
        assert "max-age=30" in resp.headers["Cache-Control"]

    def test_returns_304_when_etag_matches(self) -> None:
        payload = {"a": 1}
        etag = compute_etag(payload)
        resp = conditional_json(_request(if_none_match=etag), payload, max_age=30)
        assert resp.status_code == 304
        assert resp.headers["ETag"] == etag
        assert resp.body == b""

    def test_wildcard_and_weak_prefix_match(self) -> None:
        payload = {"a": 1}
        etag = compute_etag(payload)
        assert conditional_json(_request("*"), payload).status_code == 304
        assert conditional_json(_request(f"W/{etag}"), payload).status_code == 304

    def test_mismatched_etag_still_200(self) -> None:
        resp = conditional_json(_request('"stale"'), {"a": 1})
        assert resp.status_code == 200


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    runs = tmp_path / "runs"
    runs.mkdir()
    app = create_app(
        token=VALID_TOKEN, capsule_dir=runs, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


class TestAnalyticsConditionalGet:
    def test_first_call_has_etag_then_304(self, client: TestClient) -> None:
        r1 = client.get(f"/api/analytics/summary?{TOKEN_Q}", headers=HEADERS)
        assert r1.status_code == 200
        etag = r1.headers.get("ETag")
        assert etag
        assert "max-age" in r1.headers.get("Cache-Control", "")

        r2 = client.get(
            f"/api/analytics/summary?{TOKEN_Q}",
            headers={**HEADERS, "If-None-Match": etag},
        )
        assert r2.status_code == 304
        assert r2.content == b""


# ---------------------------------------------------------------------------
# B3: conditional GET extended to the hot polled read endpoints.
# ---------------------------------------------------------------------------

HOT_POLLED_ENDPOINTS = [
    "/api/stats",
    "/api/runs",
    "/api/alerts/recent",
    "/api/incidents",
    "/api/evidence",
    "/api/kg/topology",
    "/api/reports/catalog",
]


class TestHotEndpointsConditionalGet:
    @pytest.mark.parametrize("path", HOT_POLLED_ENDPOINTS)
    def test_200_with_etag_then_304(self, client: TestClient, path: str) -> None:
        r1 = client.get(f"{path}?{TOKEN_Q}", headers=HEADERS)
        assert r1.status_code == 200, f"{path}: {r1.status_code}"
        etag = r1.headers.get("ETag")
        assert etag, f"{path} has no ETag"
        assert "max-age" in r1.headers.get("Cache-Control", ""), path

        r2 = client.get(
            f"{path}?{TOKEN_Q}", headers={**HEADERS, "If-None-Match": etag}
        )
        assert r2.status_code == 304, f"{path}: expected 304, got {r2.status_code}"
        assert r2.content == b"", path

    @pytest.mark.parametrize("path", HOT_POLLED_ENDPOINTS)
    def test_stale_etag_still_200(self, client: TestClient, path: str) -> None:
        r = client.get(
            f"{path}?{TOKEN_Q}", headers={**HEADERS, "If-None-Match": '"stale"'}
        )
        assert r.status_code == 200, path
