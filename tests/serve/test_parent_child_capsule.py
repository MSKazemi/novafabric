"""Tests for parent/child capsule hierarchy fields in /api/runs/{run_id} (DD-1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "testtoken"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client_with_capsules(tmp_path: Path) -> tuple[TestClient, Path]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db_path = tmp_path / "registry.db"
    app = create_app(token=TOKEN, capsule_dir=capsule_dir, db_path=db_path)
    client = TestClient(app)
    return client, capsule_dir


def _make_capsule(capsule_dir: Path, run_id: str, extra: dict) -> None:
    cdir = capsule_dir / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "status": "success",
        "command": ["nova", "capture", "echo", "test"],
        "created_at": "2026-01-01T00:00:00",
        **extra,
    }
    (cdir / "capsule.yaml").write_text(yaml.dump(manifest))


def test_parent_capsule_has_worker_run_ids(client_with_capsules: tuple) -> None:
    client, capsule_dir = client_with_capsules
    worker_ids = ["worker-run-001", "worker-run-002"]
    _make_capsule(capsule_dir, "parent-run-001", {
        "capsule_type": "parent",
        "worker_run_ids": worker_ids,
    })
    resp = client.get("/api/runs/parent-run-001", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    manifest = data["manifest"]
    assert manifest.get("capsule_type") == "parent"
    assert manifest.get("worker_run_ids") == worker_ids


def test_worker_capsule_has_parent_run_id(client_with_capsules: tuple) -> None:
    client, capsule_dir = client_with_capsules
    _make_capsule(capsule_dir, "worker-run-001", {
        "capsule_type": "worker",
        "parent_run_id": "parent-run-001",
    })
    resp = client.get("/api/runs/worker-run-001", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    manifest = data["manifest"]
    assert manifest.get("capsule_type") == "worker"
    assert manifest.get("parent_run_id") == "parent-run-001"


def test_single_capsule_has_no_hierarchy_fields(client_with_capsules: tuple) -> None:
    client, capsule_dir = client_with_capsules
    _make_capsule(capsule_dir, "single-run-001", {
        "capsule_type": "single",
    })
    resp = client.get("/api/runs/single-run-001", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    manifest = data["manifest"]
    assert manifest.get("capsule_type") == "single"
    assert "worker_run_ids" not in manifest
    assert "parent_run_id" not in manifest


def test_capsule_without_type_field(client_with_capsules: tuple) -> None:
    client, capsule_dir = client_with_capsules
    _make_capsule(capsule_dir, "legacy-run-001", {})
    resp = client.get("/api/runs/legacy-run-001", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    manifest = data["manifest"]
    assert "capsule_type" not in manifest


def test_parent_capsule_partially_complete_workers(client_with_capsules: tuple) -> None:
    """Parent manifest with worker_run_ids passes through and is accessible."""
    client, capsule_dir = client_with_capsules
    worker_ids = ["worker-001", "worker-002", "worker-003"]
    _make_capsule(capsule_dir, "parent-partial-001", {
        "capsule_type": "parent",
        "worker_run_ids": worker_ids,
        "status": "PARTIALLY_COMPLETE",
    })
    resp = client.get("/api/runs/parent-partial-001", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    manifest = data["manifest"]
    assert manifest.get("capsule_type") == "parent"
    assert len(manifest.get("worker_run_ids", [])) == 3
    assert manifest.get("status") == "PARTIALLY_COMPLETE"
