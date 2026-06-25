"""Tests for federation/shard_query.py (C-2) and federation/tokens.py (C-3)."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import novafabric.trust as _trust
from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore
from novafabric.lineage.federation.shard_query import create_shard_app
from novafabric.lineage.federation.tokens import verify_cross_site_token

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_store(tmp_path: Path) -> SqliteLineageStore:
    """Return a SqliteLineageStore seeded with 5 edges."""
    store = SqliteLineageStore(db_path=tmp_path / "lineage.db", site_id="site-alpha")
    edges = [
        LineageEdge(
            edge_type="contains",
            source={"kind": "run", "run_id": "run-A"},
            target={"kind": "run", "run_id": "run-B"},
            confidence="high",
            capsule_run_id="cap-001",
        ),
        LineageEdge(
            edge_type="spawned",
            source={"kind": "run", "run_id": "run-B"},
            target={"kind": "run", "run_id": "run-C"},
            confidence="high",
            capsule_run_id="cap-001",
        ),
        LineageEdge(
            edge_type="delegated_to",
            source={"kind": "run", "run_id": "run-A"},
            target={"kind": "run", "run_id": "run-C"},
            confidence="medium",
            capsule_run_id="cap-001",
        ),
        LineageEdge(
            edge_type="replayed_from",
            source={"kind": "run", "run_id": "run-C"},
            target={"kind": "run", "run_id": "run-D"},
            confidence="high",
            capsule_run_id="cap-002",
        ),
        LineageEdge(
            edge_type="contains",
            source={"kind": "run", "run_id": "run-B"},
            target={"kind": "run", "run_id": "run-D"},
            confidence="high",
            capsule_run_id="cap-002",
        ),
    ]
    for e in edges:
        store.insert(e)
    return store


@pytest.fixture()
def shard_client(seeded_store: SqliteLineageStore) -> TestClient:
    app = create_shard_app(store=seeded_store, site_id="site-alpha")
    return TestClient(app)


def _shard_req(
    query_type: str,
    run_id: str,
    max_depth: int = 5,
    site_id: str = "coordinator",
) -> dict:
    return {
        "query_type": query_type,
        "root_run_id": run_id,
        "max_depth": max_depth,
        "site_id": site_id,
    }


# ---------------------------------------------------------------------------
# C-2 tests
# ---------------------------------------------------------------------------


def test_shard_local_query_provenance(shard_client: TestClient) -> None:
    resp = shard_client.post(
        "/lineage/shard-local-query",
        json=_shard_req("provenance", "run-A"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["site_id"] == "site-alpha"
    rows = body["rows"]
    assert isinstance(rows, list)
    assert len(rows) > 0


def test_shard_local_query_blast_radius(shard_client: TestClient) -> None:
    resp = shard_client.post(
        "/lineage/shard-local-query",
        json=_shard_req("blast_radius", "run-D"),
    )
    assert resp.status_code == 200
    body = resp.json()
    rows = body["rows"]
    assert isinstance(rows, list)
    assert len(rows) > 0


def test_shard_local_query_replay_chain(shard_client: TestClient) -> None:
    resp = shard_client.post(
        "/lineage/shard-local-query",
        json=_shard_req("replay_chain", "run-C"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["site_id"] == "site-alpha"


def test_rows_contain_node_row_keys(shard_client: TestClient) -> None:
    """Each row must have node_id, kind, ref — the NodeRow contract."""
    resp = shard_client.post(
        "/lineage/shard-local-query",
        json=_shard_req("provenance", "run-A"),
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) > 0
    for row in rows:
        assert "node_id" in row, f"Missing node_id in row: {row}"
        assert "kind" in row, f"Missing kind in row: {row}"
        assert "ref" in row, f"Missing ref in row: {row}"


def test_rows_do_not_contain_raw_edge_fields(shard_client: TestClient) -> None:
    """FR-10: response rows must NOT expose source_run_id or target_run_id."""
    resp = shard_client.post(
        "/lineage/shard-local-query",
        json=_shard_req("provenance", "run-A"),
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    for row in rows:
        assert "source_run_id" not in row, f"Sovereignty violation: {row}"
        assert "target_run_id" not in row, f"Sovereignty violation: {row}"


def test_response_has_no_edges_field(shard_client: TestClient) -> None:
    """ShardLocalResponse must not have an 'edges' field."""
    resp = shard_client.post(
        "/lineage/shard-local-query",
        json=_shard_req("provenance", "run-A"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "edges" not in body, "Response must not contain raw 'edges' field"


def test_unknown_run_returns_empty_rows(shard_client: TestClient) -> None:
    resp = shard_client.post(
        "/lineage/shard-local-query",
        json=_shard_req("provenance", "nonexistent-run"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["partial"] is False


# ---------------------------------------------------------------------------
# C-3 tests: verify_cross_site_token
# ---------------------------------------------------------------------------


def test_verify_cross_site_token_returns_bool_on_any_input() -> None:
    result = verify_cross_site_token("ns1:some-token")
    assert isinstance(result, bool)


def test_verify_cross_site_token_does_not_raise_on_empty() -> None:
    result = verify_cross_site_token("")
    assert isinstance(result, bool)


def test_verify_cross_site_token_does_not_raise_on_garbage() -> None:
    result = verify_cross_site_token("!!!not-a-token!!!")
    assert isinstance(result, bool)


def test_verify_cross_site_token_accepts_trust_root() -> None:
    result = verify_cross_site_token("ns1:token", trust_root=b"\x00" * 32)
    assert isinstance(result, bool)


def test_verify_cross_site_token_novaseal_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = sys.modules.pop("novafabric.trust.novaseal", None)
    # Make the import inside the function fail by replacing with a broken sentinel
    original_import = builtins.__import__

    def _bad_import(name: str, *args, **kwargs):
        if name == "novafabric.trust.novaseal" or name.endswith("novaseal"):
            raise ImportError("novaseal not available")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _bad_import)
    try:
        result = verify_cross_site_token("ns1:token")
        assert result is False
    finally:
        if saved is not None:
            sys.modules["novafabric.trust.novaseal"] = saved


def test_verify_cross_site_token_no_verify_attr() -> None:
    """novaseal importable but has no verify() function → returns False."""
    fake_novaseal = MagicMock(spec=[])  # no verify attribute
    with patch.object(_trust, "novaseal", fake_novaseal):
        result = verify_cross_site_token("ns1:token")
    assert result is False


def test_verify_cross_site_token_verify_returns_true() -> None:
    """When novaseal.verify() returns truthy, the function returns True."""
    fake_novaseal = MagicMock()
    fake_novaseal.verify = MagicMock(return_value=True)
    with patch.object(_trust, "novaseal", fake_novaseal):
        result = verify_cross_site_token("ns1:good-token")
    assert result is True


def test_verify_cross_site_token_verify_with_trust_root_true() -> None:
    """trust_root path calls verify_fn(sig, trust_root=...)."""
    fake_novaseal = MagicMock()
    fake_novaseal.verify = MagicMock(return_value=True)
    with patch.object(_trust, "novaseal", fake_novaseal):
        result = verify_cross_site_token("ns1:token", trust_root=b"\xde\xad\xbe\xef")
    assert result is True
    fake_novaseal.verify.assert_called_once_with("ns1:token", trust_root=b"\xde\xad\xbe\xef")
