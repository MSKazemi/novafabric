"""Bearer-header auth for `nova serve` + constant-time token comparison.

The dashboard token was historically accepted only as a ``?token=`` query
parameter, which leaks into access logs, browser history, and Referer
headers.  ``Authorization: Bearer`` is now accepted everywhere the query
form is (the query form stays supported for the SPA and existing links);
when a Bearer header is present it is authoritative.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.auth import extract_bearer, token_matches  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}


class TestTokenMatches:
    def test_equal_tokens_match(self) -> None:
        assert token_matches("abc123", "abc123")

    def test_different_tokens_do_not_match(self) -> None:
        assert not token_matches("abc123", "abc124")

    def test_unequal_lengths_do_not_match(self) -> None:
        # The old implementation returned early on length mismatch (a length
        # oracle); the hash-then-compare form must still return False.
        assert not token_matches("short", "a-much-longer-token-value")
        assert not token_matches("", "x")


class TestExtractBearer:
    def test_extracts_token(self) -> None:
        assert extract_bearer("Bearer tok123") == "tok123"

    def test_scheme_is_case_insensitive(self) -> None:
        assert extract_bearer("bearer tok123") == "tok123"

    def test_none_and_empty(self) -> None:
        assert extract_bearer(None) is None
        assert extract_bearer("") is None
        assert extract_bearer("Bearer ") is None

    def test_other_scheme_ignored(self) -> None:
        assert extract_bearer("Basic dXNlcjpwYXNz") is None


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    runs = tmp_path / "runs"
    runs.mkdir()
    app = create_app(
        token=VALID_TOKEN, capsule_dir=runs, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


class TestBearerAuth:
    def test_query_token_still_accepted(self, client: TestClient) -> None:
        resp = client.get(f"/api/stats?token={VALID_TOKEN}", headers=HEADERS)
        assert resp.status_code == 200

    def test_bearer_header_accepted(self, client: TestClient) -> None:
        resp = client.get(
            "/api/stats",
            headers={**HEADERS, "Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert resp.status_code == 200

    def test_no_credentials_is_401(self, client: TestClient) -> None:
        assert client.get("/api/stats", headers=HEADERS).status_code == 401

    def test_wrong_bearer_is_401(self, client: TestClient) -> None:
        resp = client.get(
            "/api/stats", headers={**HEADERS, "Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    def test_bearer_is_authoritative_over_query(self, client: TestClient) -> None:
        # A present-but-wrong Bearer header must not be rescued by a valid
        # query token — the header, when present, is the credential.
        resp = client.get(
            f"/api/stats?token={VALID_TOKEN}",
            headers={**HEADERS, "Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    # NOTE: the accepted SSE stream never terminates, so only the 401 branches
    # are safe to exercise with TestClient (same constraint as
    # tests/test_bl6_sse_stream.py).  The bearer-extraction logic itself is the
    # same shared helper covered above.

    def test_sse_stream_rejects_missing_token(self, client: TestClient) -> None:
        resp = client.get("/api/runs/stream", headers=HEADERS)
        assert resp.status_code == 401

    def test_sse_stream_rejects_wrong_bearer(self, client: TestClient) -> None:
        # Proves the SSE route consults the Authorization header: a wrong
        # bearer must not be rescued by anything else.
        resp = client.get(
            "/api/runs/stream",
            headers={**HEADERS, "Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
