"""Dashboard serve endpoints for the Accountability Spine features.

GET /api/runs/{id}/energy   (ADR-0093)
GET /api/runs/{id}/ledger   (ADR-0094)
GET /api/runs/{id}/safety-case (ADR-0095)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.capture.orchestrator import CaptureOrchestrator  # noqa: E402
from novafabric.energy._attribution import attest_capsule, write_receipts  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-spine"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}


@pytest.fixture
def run_id(tmp_path: Path) -> str:
    cap = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "pass"]
    ).capsule_dir
    # give the capsule one action so attribution produces a receipt
    (cap / "model-calls.jsonl").write_text(
        '{"model_call_id":"01HXAY7M5JZ8R7K4P9DPBYK2WX","model":"gpt-4"}\n'
    )
    write_receipts(cap, attest_capsule(cap, rapl_base=tmp_path / "no-rapl", node_id="t"))
    return cap.name


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova-home"))
    app = create_app(
        capsule_dir=tmp_path / "runs",
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def test_energy_endpoint_returns_receipts(client, run_id):
    r = client.get(f"/api/runs/{run_id}/energy?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert len(body["receipts"]) >= 1
    assert body["conservation"]["status"] in ("balanced", "diverged", "unmeasurable")


def test_ledger_endpoint_reports_no_checkpoint(client, run_id):
    r = client.get(f"/api/runs/{run_id}/ledger?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    body = r.json()
    # the capsule was never anchored → honest NO_CHECKPOINT
    assert body["status"] == "NO_CHECKPOINT"
    assert body["ok"] is False


def test_safety_case_endpoint_builds_tree(client, run_id):
    r = client.get(
        f"/api/runs/{run_id}/safety-case?{TOKEN_Q}&template=clymer-generic-v0", headers=H
    )
    assert r.status_code == 200
    body = r.json()
    assert body["template_id"] == "clymer-generic-v0"
    assert len(body["nodes"]) >= 1


def test_safety_case_endpoint_rejects_unknown_template(client, run_id):
    r = client.get(
        f"/api/runs/{run_id}/safety-case?{TOKEN_Q}&template=does-not-exist", headers=H
    )
    assert r.status_code == 400
