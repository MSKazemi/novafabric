"""Tests for v0.19.0 run-utility routes.

Covers:
  POST /api/kg/init              — KG schema init
  POST /api/kg/ingest            — KG capsule ingest
  GET  /api/admin/new-run-id     — generate fresh ULID
  GET  /api/runs/{id}/children   — parent/child hierarchy
  POST /api/runs/{id}/validate-distributed — distributed capsule validation
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}  # required by DNS-rebinding guard
HT = {**H, "host": "127.0.0.1:4321"}  # alias


def _make_manifest(run_id: str, **kwargs: object) -> dict:
    base: dict = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.19.0",
        "run_id": run_id,
        "created_at": "2026-05-18T00:00:00+00:00",
        "finished_at": "2026-05-18T00:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print(1)"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    base.update(kwargs)
    return base


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/admin/new-run-id
# ---------------------------------------------------------------------------

def test_new_run_id_returns_ulid(client: TestClient) -> None:
    r = client.get(f"/api/admin/new-run-id?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert "run_id" in data
    assert len(data["run_id"]) == 26  # ULID length
    assert "NOVAFABRIC_GLOBAL_RUN_ID" in data["env_var"]
    assert data["run_id"] in data["cli_cmd"]


def test_new_run_id_unique(client: TestClient) -> None:
    ids = {
        client.get(f"/api/admin/new-run-id?{TOKEN_Q}", headers=H).json()["run_id"]
        for _ in range(5)
    }
    assert len(ids) == 5


def test_new_run_id_requires_auth(client: TestClient) -> None:
    r = client.get("/api/admin/new-run-id", headers=H)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/kg/init
# ---------------------------------------------------------------------------

def test_kg_init_returns_ok_or_import_error(client: TestClient) -> None:
    """Without novafabric[scale-kg], endpoint returns ok=False gracefully."""
    r = client.post(f"/api/kg/init?{TOKEN_Q}", json={}, headers=H)
    assert r.status_code == 200
    data = r.json()
    assert "ok" in data
    assert "db_path" in data


def test_kg_init_requires_auth(client: TestClient) -> None:
    r = client.post("/api/kg/init", json={}, headers=H)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/kg/ingest
# ---------------------------------------------------------------------------

def test_kg_ingest_missing_capsule_path(client: TestClient) -> None:
    r = client.post(f"/api/kg/ingest?{TOKEN_Q}", json={}, headers=H)
    assert r.status_code == 422


def test_kg_ingest_nonexistent_dir(client: TestClient, capsule_dir: Path) -> None:
    r = client.post(
        f"/api/kg/ingest?{TOKEN_Q}",
        json={"capsule_path": str(capsule_dir / "ghost")},
        headers=H,
    )
    assert r.status_code == 404


def test_kg_ingest_no_events_file(client: TestClient, capsule_dir: Path) -> None:
    run_id = "01ATEST000000000000000001"
    cdir = capsule_dir / run_id
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.dump(_make_manifest(run_id)))
    r = client.post(
        f"/api/kg/ingest?{TOKEN_Q}",
        json={"capsule_path": str(cdir)},
        headers=H,
    )
    assert r.status_code == 200
    data = r.json()
    # No KG package or no events file — either fails gracefully
    assert "ok" in data


def test_kg_ingest_requires_auth(client: TestClient, capsule_dir: Path) -> None:
    r = client.post("/api/kg/ingest", json={"capsule_path": str(capsule_dir)}, headers=H)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/children
# ---------------------------------------------------------------------------

def test_run_children_not_found(client: TestClient) -> None:
    r = client.get(f"/api/runs/DOESNOTEXIST/children?{TOKEN_Q}", headers=H)
    assert r.status_code == 404


def test_run_children_no_children(client: TestClient, capsule_dir: Path) -> None:
    run_id = "01PARENT0000000000000001"
    cdir = capsule_dir / run_id
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.dump(_make_manifest(run_id, capsule_type="parent")))
    r = client.get(f"/api/runs/{run_id}/children?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["run_id"] == run_id
    assert data["child_count"] == 0
    assert data["children"] == []


def test_run_children_with_worker(client: TestClient, capsule_dir: Path) -> None:
    parent_id = "01PARENT0000000000000002"
    worker_id = "01WORKER0000000000000002"

    parent_dir = capsule_dir / parent_id
    parent_dir.mkdir()
    (parent_dir / "capsule.yaml").write_text(
        yaml.dump(_make_manifest(parent_id, capsule_type="parent"))
    )

    worker_dir = capsule_dir / worker_id
    worker_dir.mkdir()
    (worker_dir / "capsule.yaml").write_text(
        yaml.dump(_make_manifest(worker_id, capsule_type="worker", parent_run_id=parent_id))
    )
    lineage_rec = {"parent_run_id": parent_id, "child_run_id": worker_id, "edge_type": "worker"}
    (worker_dir / "lineage.jsonl").write_text(json.dumps(lineage_rec) + "\n")

    r = client.get(f"/api/runs/{parent_id}/children?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["child_count"] == 1
    assert data["children"][0]["run_id"] == worker_id
    assert data["children"][0]["edge_type"] == "worker"


def test_run_children_requires_auth(client: TestClient, capsule_dir: Path) -> None:
    run_id = "01PARENT0000000000000003"
    cdir = capsule_dir / run_id
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.dump(_make_manifest(run_id)))
    r = client.get(f"/api/runs/{run_id}/children", headers=H)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/validate-distributed
# ---------------------------------------------------------------------------

def test_validate_distributed_not_found(client: TestClient) -> None:
    r = client.post(f"/api/runs/NOSUCHRUN/validate-distributed?{TOKEN_Q}", json={}, headers=H)
    assert r.status_code == 404


def test_validate_distributed_requires_auth(client: TestClient, capsule_dir: Path) -> None:
    run_id = "01PARENT0000000000000004"
    cdir = capsule_dir / run_id
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.dump(_make_manifest(run_id)))
    r = client.post(f"/api/runs/{run_id}/validate-distributed", json={}, headers=H)
    assert r.status_code == 401


def test_validate_distributed_no_validator(client: TestClient, capsule_dir: Path) -> None:
    """If DistributedCapsuleValidator is missing, endpoint returns 501 or graceful error."""
    run_id = "01PARENT0000000000000005"
    cdir = capsule_dir / run_id
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.dump(_make_manifest(run_id, capsule_type="parent")))
    r = client.post(
        f"/api/runs/{run_id}/validate-distributed?{TOKEN_Q}", json={}, headers=H
    )
    assert r.status_code in (200, 501)
    if r.status_code == 200:
        data = r.json()
        assert "ok" in data


# ---------------------------------------------------------------------------
# POST /api/kg/ingest-all
# ---------------------------------------------------------------------------

def test_kg_ingest_all_empty_capsule_dir(client: TestClient, capsule_dir: Path) -> None:
    """Returns ok=True with total=0 when capsule dir has no subdirs (kuzu optional)."""
    r = client.post(
        f"/api/kg/ingest-all?{TOKEN_Q}",
        json={"capsule_base": str(capsule_dir)},
        headers=H,
    )
    assert r.status_code == 200
    data = r.json()
    # Without kuzu: ok=False with graceful error. With kuzu: ok=True, total=0.
    assert "ok" in data
    assert "total" in data or "error" in data


def test_kg_ingest_all_with_capsule(client: TestClient, capsule_dir: Path) -> None:
    """Scans the capsule base; returns ok+total when kuzu available, graceful error otherwise."""
    run_id = "01KGALL00000000000000001"
    cdir = capsule_dir / run_id
    cdir.mkdir()
    (cdir / "model-calls.jsonl").write_text(
        '{"event_type":"ModelCallCompleted","agent_id":"a1","model_id":"gpt-4o",'
        '"input_tokens":10,"output_tokens":5}\n',
        encoding="utf-8",
    )
    r = client.post(
        f"/api/kg/ingest-all?{TOKEN_Q}",
        json={"capsule_base": str(capsule_dir)},
        headers=H,
    )
    assert r.status_code == 200
    data = r.json()
    assert "ok" in data
    if data["ok"]:
        assert data["total"] >= 1


def test_kg_ingest_all_requires_auth(client: TestClient, capsule_dir: Path) -> None:
    """Returns 401 without auth token."""
    r = client.post(
        "/api/kg/ingest-all",
        json={"capsule_base": str(capsule_dir)},
        headers=H,
    )
    assert r.status_code == 401
