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
"""ADR-0206 P2: keyset (seek) pagination for ``GET /v0/lineage/nodes``."""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import PaginationConfig, ServerConfig  # noqa: E402
from novafabric.server.pagination import (  # noqa: E402
    encode_cursor,
    encode_keyset_cursor,
)


def _seed(db_path: Path, refs: list[tuple[str, str]], run_id: str = "seed") -> list[str]:
    from novafabric.lineage._store import LineageStore
    from novafabric.lineage._types import LineageNode, node_id_for

    nodes = [
        LineageNode(
            node_id=node_id_for(kind, ref),
            kind=kind,
            ref=ref,
            first_seen_capsule_run_id=run_id,
            payload={"ref": ref},
        )
        for kind, ref in refs
    ]
    LineageStore(db_path=db_path).replace_capsule_lineage(nodes, [], run_id)
    return [n.node_id for n in nodes]


def _client(tmp_path: Path, *, legacy: bool = True) -> tuple[TestClient, Path]:
    db_path = tmp_path / "test.db"
    cfg = ServerConfig(
        db_path=str(db_path),
        insecure_no_auth=True,
        pagination=PaginationConfig(legacy_offset_cursors=legacy),
    )
    return TestClient(create_app(cfg), raise_server_exceptions=False), db_path


def _walk(client: TestClient, limit: int, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(1000):
        q: dict[str, Any] = {"limit": limit, **params}
        if cursor:
            q["cursor"] = cursor
        resp = client.get("/v0/lineage/nodes", params=q)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("pagination did not terminate")


def test_walk_matches_full_sorted_scan(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path)
    rng = random.Random(206)
    refs = [(rng.choice(["run", "asset", "dataset"]), f"ref-{i}") for i in range(37)]
    ids = _seed(db_path, refs)
    for limit in (1, 5, 36, 37, 500):
        got = [n["node_id"] for n in _walk(client, limit)]
        assert got == sorted(ids), limit


def test_kind_filter_walk(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path)
    refs = [("run" if i % 3 else "asset", f"r{i}") for i in range(20)]
    _seed(db_path, refs)
    got = _walk(client, 3, kind="asset")
    assert got and all(n["kind"] == "asset" for n in got)
    assert len(got) == sum(1 for k, _ in refs if k == "asset")
    assert [n["node_id"] for n in got] == sorted(n["node_id"] for n in got)


def test_first_page_has_total_keyset_pages_omit_it(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path)
    _seed(db_path, [("run", f"r{i}") for i in range(5)])
    first = client.get("/v0/lineage/nodes", params={"limit": 2}).json()
    assert first["total"] == 5
    second = client.get(
        "/v0/lineage/nodes", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert "total" not in second
    assert len(second["items"]) == 2


def test_cursor_is_shared_v1_format(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path)
    _seed(db_path, [("run", f"r{i}") for i in range(3)])
    body = client.get("/v0/lineage/nodes", params={"limit": 1}).json()
    raw = body["next_cursor"]
    data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert data == {"v": 1, "k": [None, body["items"][0]["node_id"]]}


def test_inserts_between_pages_do_not_duplicate_or_skip(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path)
    ids = _seed(db_path, [("run", f"a{i}") for i in range(10)], run_id="one")
    first = client.get("/v0/lineage/nodes", params={"limit": 4}).json()
    seen = [n["node_id"] for n in first["items"]]
    # A new capsule adds nodes whose ids sort before AND after the cursor.
    new_ids = _seed(db_path, [("asset", f"b{i}") for i in range(10)], run_id="two")
    cursor = first["next_cursor"]
    while cursor:
        body = client.get("/v0/lineage/nodes", params={"limit": 4, "cursor": cursor}).json()
        seen.extend(n["node_id"] for n in body["items"])
        cursor = body["next_cursor"]
    assert len(seen) == len(set(seen)), "duplicate rows across pages"
    assert seen == sorted(seen)
    assert set(ids) <= set(seen), "an original row was skipped"
    # Only new ids sorting after the first page's last key can appear.
    boundary = first["items"][-1]["node_id"]
    assert {i for i in new_ids if i > boundary} <= set(seen)


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64-$$$",
        base64.urlsafe_b64encode(b"[1,2]").decode(),
        base64.urlsafe_b64encode(json.dumps({"v": 2, "k": [None, "x"]}).encode()).decode(),
        base64.urlsafe_b64encode(json.dumps({"v": 1, "k": [None]}).encode()).decode(),
        encode_keyset_cursor("2026-01-01T00:00:00Z", "run-1"),  # a capsules cursor
    ],
)
def test_bad_cursor_is_400(tmp_path: Path, cursor: str) -> None:
    client, db_path = _client(tmp_path)
    _seed(db_path, [("run", "r")])
    resp = client.get("/v0/lineage/nodes", params={"cursor": cursor})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_legacy_offset_cursor_served_with_deprecation(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path)
    ids = sorted(_seed(db_path, [("run", f"r{i}") for i in range(6)]))
    resp = client.get("/v0/lineage/nodes", params={"limit": 2, "cursor": encode_cursor(2)})
    assert resp.status_code == 200
    assert resp.headers.get("Deprecation") == "true"
    body = resp.json()
    assert [n["node_id"] for n in body["items"]] == ids[2:4]
    assert body["total"] == 6
    assert body["next_cursor"] == encode_cursor(4)


def test_legacy_offset_cursor_refused_after_sunset(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path, legacy=False)
    _seed(db_path, [("run", "r")])
    resp = client.get("/v0/lineage/nodes", params={"cursor": encode_cursor(1)})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_empty_store_shape_unchanged(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    body = client.get("/v0/lineage/nodes").json()
    assert body == {"items": [], "next_cursor": None, "total": 0}
