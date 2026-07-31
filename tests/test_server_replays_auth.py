"""Replay read routes require auth (audit fix).

POST/DELETE on /v0/replays were writer-gated from the start, but the two GET
routes (status + SSE events) had no auth dependency at all — any anonymous
client could read replay status/results on a token- or OIDC-protected server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

TEST_TOKEN = "replays-auth-test-token"


def _client(tmp_path: Path, **cfg_kwargs: object) -> TestClient:
    cfg = ServerConfig(db_path=str(tmp_path / "test.db"), **cfg_kwargs)  # type: ignore[arg-type]
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestReplayReadAuth:
    def test_get_replay_without_token_is_401(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.get("/v0/replays/any-id")
        assert resp.status_code == 401

    def test_get_replay_events_without_token_is_401(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.get("/v0/replays/any-id/events")
        assert resp.status_code == 401

    def test_get_replay_with_token_reaches_handler(self, tmp_path: Path) -> None:
        # Authenticated request passes auth and hits the job store (404 for
        # an unknown id — not 401).
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.get("/v0/replays/no-such-id", headers=_bearer(TEST_TOKEN))
        assert resp.status_code == 404

    def test_insecure_no_auth_mode_still_works(self, tmp_path: Path) -> None:
        # The anonymous-admin opt-out must keep working for the existing
        # test-suite/demo flows.
        client = _client(tmp_path, insecure_no_auth=True)
        resp = client.get("/v0/replays/no-such-id")
        assert resp.status_code == 404
