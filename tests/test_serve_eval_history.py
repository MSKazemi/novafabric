"""Tests for GET /api/assets/{id}/eval-history."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import _extract_score, create_app

# --- _extract_score unit tests ---

def test_extract_score_none() -> None:
    assert _extract_score(None) is None


def test_extract_score_bare_float() -> None:
    assert _extract_score("0.91") == pytest.approx(0.91)


def test_extract_score_bare_int() -> None:
    assert _extract_score("1") == 1.0


def test_extract_score_dict_score_key() -> None:
    assert _extract_score(json.dumps({"score": 0.75, "other": "x"})) == pytest.approx(0.75)


def test_extract_score_invalid_json() -> None:
    assert _extract_score("not-json") is None


def test_extract_score_dict_no_score_key() -> None:
    assert _extract_score(json.dumps({"accuracy": 0.9})) is None


# --- integration tests ---

@pytest.fixture()
def client_with_asset(tmp_path: Path) -> tuple[TestClient, str]:
    from novafabric.registry.store import get_connection, init_schema

    db_path = tmp_path / "registry.db"
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO assets (id, name, version, asset_type, status, created_at, spec_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("asset-1", "my-agent", "1.0.0", "agent", "staging", "2026-05-12T00:00:00Z", "{}"),
    )
    conn.execute(
        "INSERT INTO eval_results (asset_id, suite_name, passed, score_json, run_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("asset-1", "smoke", 1, "0.91", "2026-05-12T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO eval_results (asset_id, suite_name, passed, score_json, run_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("asset-1", "smoke", 0, "0.60", "2026-05-11T10:00:00Z"),
    )
    conn.commit()
    conn.close()

    app = create_app(token="tok", capsule_dir=tmp_path, db_path=db_path)
    return TestClient(app, base_url="http://localhost"), "asset-1"


def test_eval_history_returns_sorted_newest_first(client_with_asset: tuple) -> None:
    client, asset_id = client_with_asset
    r = client.get(f"/api/assets/{asset_id}/eval-history", params={"token": "tok"})
    assert r.status_code == 200
    data = r.json()
    assert data["asset_id"] == asset_id
    assert len(data["history"]) == 2
    assert data["history"][0]["passed"] is True   # 2026-05-12 is newer
    assert data["history"][1]["passed"] is False
    assert data["history"][0]["score"] == pytest.approx(0.91)


def test_eval_history_limit_respected(client_with_asset: tuple) -> None:
    client, asset_id = client_with_asset
    r = client.get(f"/api/assets/{asset_id}/eval-history", params={"token": "tok", "limit": 1})
    assert r.status_code == 200
    assert len(r.json()["history"]) == 1


def test_eval_history_404_unknown_asset(client_with_asset: tuple) -> None:
    client, _ = client_with_asset
    r = client.get("/api/assets/nonexistent/eval-history", params={"token": "tok"})
    assert r.status_code == 404


# ---------- GET /api/assets/{asset_id} — get_asset_by_id_endpoint ----------

def test_get_asset_by_id_returns_asset_with_eval_results(client_with_asset: tuple) -> None:
    client, asset_id = client_with_asset
    r = client.get(f"/api/assets/{asset_id}", params={"token": "tok"})
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == asset_id
    assert data["name"] == "my-agent"
    assert "eval_results" in data
    assert len(data["eval_results"]) == 2


def test_get_asset_by_id_404_unknown(client_with_asset: tuple) -> None:
    client, _ = client_with_asset
    r = client.get("/api/assets/no-such-id", params={"token": "tok"})
    assert r.status_code == 404


# ---------- POST /api/assets/{asset_id}/eval — eval_asset_by_id_endpoint ----------

def test_eval_asset_by_id_unconfirmed_returns_400(client_with_asset: tuple) -> None:
    client, asset_id = client_with_asset
    r = client.post(
        f"/api/assets/{asset_id}/eval",
        params={"token": "tok"},
        json={"suite": "novafabric-smoke-v1", "confirmed": False},
    )
    assert r.status_code == 400
    assert "confirmed" in r.json()["detail"]


def test_eval_asset_by_id_404_unknown_asset(client_with_asset: tuple) -> None:
    client, _ = client_with_asset
    r = client.post(
        "/api/assets/ghost-id/eval",
        params={"token": "tok"},
        json={"suite": "novafabric-smoke-v1", "confirmed": True},
    )
    assert r.status_code == 404
