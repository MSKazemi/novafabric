"""Serve route tests for POST /api/kg/detect — SPKG anomaly scan (ADR-0111).

Dashboard-parity server surface for `nova kg detect`. Pure-stdlib detector, so no
optional extra is required; uses fastapi TestClient (no uvicorn needed).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

VALID_TOKEN = "test-token-kg-detect-xyz"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}  # satisfy the DNS-rebinding localhost host guard


def _write_lineage(capsule: Path) -> None:
    capsule.mkdir(parents=True, exist_ok=True)
    ts = "2026-07-02T14:00:00.000000Z"
    edges = [
        {"edge_type": "uses", "source": {"kind": "run", "ref": f"run-{i}"},
         "target": {"kind": "dataset", "ref": "dataset:training-set"},
         "created_at": ts, "capsule_run_id": "run-x"}
        for i in range(20)
    ]
    edges.append(
        {"edge_type": "executes", "source": {"kind": "run", "ref": "run-evil"},
         "target": {"kind": "tool", "ref": "tool:/bin/shell"},
         "created_at": ts, "capsule_run_id": "run-x"}
    )
    (capsule / "lineage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in edges) + "\n", encoding="utf-8"
    )


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    return base


@pytest.fixture()
def client(capsule_dir: Path, tmp_path: Path) -> TestClient:
    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    return TestClient(app)


def test_detect_ranks_shell_edge(client: TestClient, capsule_dir: Path) -> None:
    cap = capsule_dir / "run-x"
    _write_lineage(cap)
    r = client.post(f"/api/kg/detect?{TOKEN_Q}", json={"capsule_path": str(cap), "top": 3}, headers=H)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["count"] == 3
    # The planted shell exec ranks first, mapped to the Unix-shell technique.
    top = data["findings"][0]
    assert top["explanation"]["attack_technique_id"] == "T1059.004"
    assert top["score"] == 1.0


def test_detect_requires_capsule_path(client: TestClient) -> None:
    r = client.post(f"/api/kg/detect?{TOKEN_Q}", json={}, headers=H)
    assert r.status_code == 422


def test_detect_missing_dir_404(client: TestClient) -> None:
    r = client.post(f"/api/kg/detect?{TOKEN_Q}", json={"capsule_path": "/nonexistent/xyzzy"}, headers=H)
    assert r.status_code == 404


def test_detect_empty_lineage_ok(client: TestClient, capsule_dir: Path) -> None:
    cap = capsule_dir / "empty-run"
    cap.mkdir()
    r = client.post(f"/api/kg/detect?{TOKEN_Q}", json={"capsule_path": str(cap)}, headers=H)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["count"] == 0
    assert data["findings"] == []


def test_detect_requires_token(client: TestClient, capsule_dir: Path) -> None:
    cap = capsule_dir / "run-x"
    _write_lineage(cap)
    r = client.post("/api/kg/detect", json={"capsule_path": str(cap)}, headers=H)
    assert r.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# /api/kg/attack-path + /api/kg/blast-radius (UC2 / UC3) — need the [spkg] extra
# --------------------------------------------------------------------------- #

pytest.importorskip("kuzu")


def _write_chain(capsule: Path) -> None:
    """run:attacker --uses--> tool:shell --produces--> dataset:secrets."""
    capsule.mkdir(parents=True, exist_ok=True)
    ts = "2026-07-02T14:00:00.000000Z"
    edges = [
        {"edge_type": "uses", "source": {"kind": "run", "ref": "attacker"},
         "target": {"kind": "tool", "ref": "shell"}, "created_at": ts, "capsule_run_id": "c"},
        {"edge_type": "produces", "source": {"kind": "tool", "ref": "shell"},
         "target": {"kind": "dataset", "ref": "secrets"}, "created_at": ts, "capsule_run_id": "c"},
    ]
    (capsule / "lineage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in edges) + "\n", encoding="utf-8"
    )


def test_attack_path_found(client: TestClient, capsule_dir: Path) -> None:
    cap = capsule_dir / "chain"
    _write_chain(cap)
    r = client.post(
        f"/api/kg/attack-path?{TOKEN_Q}",
        json={"capsule_path": str(cap), "from_entity": "run:attacker",
              "to_entity": "dataset:secrets"},
        headers=H,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["path_found"] is True
    assert data["hops"] == 2


def test_attack_path_none(client: TestClient, capsule_dir: Path) -> None:
    cap = capsule_dir / "chain"
    _write_chain(cap)
    r = client.post(
        f"/api/kg/attack-path?{TOKEN_Q}",
        json={"capsule_path": str(cap), "from_entity": "dataset:secrets",
              "to_entity": "run:attacker"},  # reverse — no directed path
        headers=H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["path_found"] is False


def test_attack_path_requires_fields(client: TestClient, capsule_dir: Path) -> None:
    cap = capsule_dir / "chain"
    _write_chain(cap)
    r = client.post(
        f"/api/kg/attack-path?{TOKEN_Q}",
        json={"capsule_path": str(cap), "from_entity": "run:attacker"},  # no to_entity
        headers=H,
    )
    assert r.status_code == 422


def test_blast_radius_downstream(client: TestClient, capsule_dir: Path) -> None:
    cap = capsule_dir / "chain"
    _write_chain(cap)
    r = client.post(
        f"/api/kg/blast-radius?{TOKEN_Q}",
        json={"capsule_path": str(cap), "entity": "run:attacker"},
        headers=H,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["direction"] == "downstream"
    refs = {e["ref"] for e in data["entities"]}
    assert {"shell", "secrets"} <= refs  # both reachable downstream


def test_blast_radius_upstream(client: TestClient, capsule_dir: Path) -> None:
    cap = capsule_dir / "chain"
    _write_chain(cap)
    r = client.post(
        f"/api/kg/blast-radius?{TOKEN_Q}",
        json={"capsule_path": str(cap), "entity": "dataset:secrets", "upstream": True},
        headers=H,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["direction"] == "upstream"
    refs = {e["ref"] for e in data["entities"]}
    assert {"shell", "attacker"} <= refs
