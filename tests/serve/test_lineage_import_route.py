"""``POST /api/lineage/import`` with a bare run id imports that capsule (review serve-06).

A run id resolves to ``<capsule_dir>/<run_id>`` — the capsule itself — but the route
handed it to ``import_capsule_dir``, which iterates *children* for capsules, so it
imported 0 edges and answered ``ok: true``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.serve.app import create_app

TOKEN = "lineage-import-token"
AUTH = {"token": TOKEN}
HOST = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    store = tmp_path / "runs"
    store.mkdir()
    return TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r.db")), store


def _capture(store: Path) -> Path:
    """A real capsule, with one lineage edge (a bare `pass` capture records none)."""
    import json

    cap = CaptureOrchestrator(base_dir=store).run(command=[sys.executable, "-c", "pass"]).capsule_dir
    edge = {
        "edge_id": f"edge-{cap.name}",
        "edge_type": "consumed",
        "source": {"kind": "run", "run_id": cap.name},
        "target": {"kind": "asset", "asset_ref": "model:gpt-4o", "registry": "local"},
        "direction": "source_to_target",
        "emitter": {"name": "test", "version": "1"},
        "confidence": "declared",
        "capsule_run_id": cap.name,
        "schema_version": "0.1.0",
        "created_at": "2026-10-24T00:00:00.000000Z",
    }
    (cap / "lineage.jsonl").write_text(json.dumps(edge) + "\n")
    return cap


def test_a_bare_run_id_imports_that_capsules_edges(stack: tuple[TestClient, Path]) -> None:
    client, store = stack
    cap = _capture(store)
    resp = client.post("/api/lineage/import", params=AUTH, headers=HOST,
                       json={"capsule_path": cap.name})
    body = resp.json()
    assert body["ok"] is True, body
    assert body["file_count"] == 1 and body["imported"] >= 1


def test_a_directory_of_capsules_still_imports_each(stack: tuple[TestClient, Path]) -> None:
    client, store = stack
    _capture(store)
    _capture(store)
    resp = client.post("/api/lineage/import", params=AUTH, headers=HOST,
                       json={"capsule_path": str(store)})
    body = resp.json()
    assert body["ok"] is True and body["file_count"] == 2, body


def test_a_directory_with_no_capsule_is_not_ok(stack: tuple[TestClient, Path]) -> None:
    client, store = stack
    (store / "empty").mkdir()
    resp = client.post("/api/lineage/import", params=AUTH, headers=HOST,
                       json={"capsule_path": "empty"})
    body = resp.json()
    assert body["ok"] is False and body["imported"] == 0, body
