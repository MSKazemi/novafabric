# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""``GET /v0/assets`` keyset pagination (ADR-0206 P2, experimental)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.registry.service import list_assets_keyset  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402
from novafabric.server.pagination import encode_cursor, encode_keyset_cursor  # noqa: E402

_MODEL = """
novafabric_spec_version: "0.1"
asset_type: model
name: {name}
version: "1.0.0"
status: development
spec:
  framework: pytorch
  artifact_path: /tmp/model.pt
"""

_AGENT = """
novafabric_spec_version: "0.1"
asset_type: agent
name: {name}
version: "1.0.0"
status: development
spec:
  model:
    provider: openai
    name: gpt-4
  tools:
    - web_search
  prompts:
    system: "You are a helpful assistant."
  policies: []
  evals:
    - basic_suite
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


def _client(db_path: Path, **cfg: Any) -> TestClient:
    config = ServerConfig(db_path=str(db_path), insecure_no_auth=True)
    for key, value in cfg.items():
        setattr(config.pagination, key, value)
    return TestClient(create_app(config), raise_server_exceptions=False)


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return _client(db_path)


def _register(client: TestClient, n: int, template: str = _MODEL, prefix: str = "m") -> None:
    for i in range(n):
        resp = client.post("/v0/assets", json={"spec_yaml": template.format(name=f"{prefix}{i}")})
        assert resp.status_code == 201, resp.text


def _tie_timestamps(db_path: Path, buckets: int) -> None:
    """Collapse created_at into a few values so the id tiebreak is exercised."""
    conn = sqlite3.connect(db_path)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM assets ORDER BY name")]
        for i, asset_id in enumerate(ids):
            stamp = f"2026-09-0{1 + i % buckets}T00:00:00+00:00"
            conn.execute("UPDATE assets SET created_at = ? WHERE id = ?", (stamp, asset_id))
        conn.commit()
    finally:
        conn.close()


def _expected(db_path: Path, where: str = "") -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM assets {where} ORDER BY created_at DESC, id DESC"
            )
        ]
    finally:
        conn.close()


def _walk(client: TestClient, limit: int, query: str = "") -> tuple[list[str], list[dict[str, Any]]]:
    ids: list[str] = []
    bodies: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(1000):
        url = f"/v0/assets?limit={limit}{query}" + (f"&cursor={cursor}" if cursor else "")
        resp = client.get(url)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        bodies.append(body)
        ids.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return ids, bodies
    raise AssertionError("walk did not terminate")


def test_walk_matches_full_order_with_ties(client: TestClient, db_path: Path) -> None:
    _register(client, 11)
    _tie_timestamps(db_path, buckets=3)
    ids, bodies = _walk(client, limit=3)
    assert ids == _expected(db_path)
    assert len(set(ids)) == 11
    assert bodies[0]["total"] == 11
    assert all("total" not in b for b in bodies[1:])


def test_filters_apply_to_every_page(client: TestClient, db_path: Path) -> None:
    _register(client, 4, _MODEL, "m")
    _register(client, 5, _AGENT, "a")
    _tie_timestamps(db_path, buckets=2)
    ids, bodies = _walk(client, limit=2, query="&asset_type=agent")
    assert ids == _expected(db_path, "WHERE asset_type = 'agent'")
    assert bodies[0]["total"] == 5


def test_registration_between_pages_does_not_shift_the_walk(
    client: TestClient, db_path: Path
) -> None:
    _register(client, 6)
    _tie_timestamps(db_path, buckets=6)
    page1 = client.get("/v0/assets?limit=3").json()
    served = [i["id"] for i in page1["items"]]
    remaining = _expected(db_path)[3:]
    _register(client, 2, prefix="late")  # newer created_at: sorts before the cursor
    rest: list[str] = []
    cursor = page1["next_cursor"]
    while cursor:
        body = client.get(f"/v0/assets?limit=3&cursor={cursor}").json()
        rest.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]
    assert rest == remaining
    assert not set(served) & set(rest)


@pytest.mark.parametrize(
    "cursor",
    ["garbage!!", encode_keyset_cursor(None, "x")[:-3] + "@@@", "eyJ2IjogOX0"],
)
def test_invalid_cursor_is_400(client: TestClient, cursor: str) -> None:
    resp = client.get(f"/v0/assets?cursor={cursor}")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_legacy_offset_cursor_still_served_with_deprecation(
    client: TestClient, db_path: Path
) -> None:
    _register(client, 5)
    resp = client.get(f"/v0/assets?limit=2&cursor={encode_cursor(2)}")
    assert resp.status_code == 200
    assert resp.headers.get("Deprecation") == "true"
    body = resp.json()
    assert len(body["items"]) == 2 and body["total"] == 5


def test_legacy_offset_cursor_refused_after_sunset(db_path: Path) -> None:
    client = _client(db_path, legacy_offset_cursors=False)
    resp = client.get(f"/v0/assets?cursor={encode_cursor(2)}")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_empty_registry(client: TestClient) -> None:
    body = client.get("/v0/assets").json()
    assert body == {"items": [], "next_cursor": None, "total": 0}


def test_service_null_key_admits_nothing(db_path: Path, client: TestClient) -> None:
    _register(client, 2)
    rows, total = list_assets_keyset(None, None, limit=10, after=(None, "z"), db_path=db_path)
    assert rows == [] and total is None


def test_service_omits_spec_json_and_clamps_limit(db_path: Path, client: TestClient) -> None:
    _register(client, 2)
    rows, total = list_assets_keyset(None, None, limit=0, with_total=True, db_path=db_path)
    assert len(rows) == 1 and total == 2
    assert "spec_json" not in rows[0]
