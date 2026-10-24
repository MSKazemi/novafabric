"""The serve audit routes scan the capsules they are about (review serve-05).

``/api/compliance/audit/report`` passed ``capsule.parent`` as ``data_dir``, so the engine
looked in ``<store>/capsules`` — a directory that does not exist — and reported
``capsule_count: 0``, ``overall_score: 0.0`` for a capsule the CLI scores 0.95. The
coverage and bundle routes used ``capsule_dir.parent / "capsules"``: a different store
whenever ``nova serve --capsule-dir`` is not ``<home>/capsules``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.serve.app import create_app

TOKEN = "audit-route-token"
AUTH = {"token": TOKEN}
HOST = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def stack(tmp_path: Path) -> tuple[TestClient, Path]:
    store = tmp_path / "runs"  # deliberately not <home>/capsules
    store.mkdir()
    app = create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "registry.db")
    return TestClient(app), store


def _capture(store: Path) -> Path:
    return CaptureOrchestrator(base_dir=store).run(command=[sys.executable, "-c", "pass"]).capsule_dir


def test_the_report_scans_exactly_the_requested_capsule(
    stack: tuple[TestClient, Path], tmp_path: Path
) -> None:
    import shutil

    from novafabric.compliance.audit.engine import AuditEngine
    from novafabric.compliance.audit.loader import load_profile

    client, store = stack
    target = _capture(store)
    _capture(store)  # another capsule in the same store
    resp = client.post(
        "/api/compliance/audit/report", params=AUTH, headers=HOST,
        json={"run_id": target.name, "profile": "nist-ai-rmf"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capsule_count"] == 1
    # The CLI's view of the same capsule, alone in a store, is the reference.
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copytree(target, alone / target.name)
    reference = AuditEngine(
        data_dir=tmp_path, profile=load_profile("nist-ai-rmf"), capsules_dir=alone
    ).scan()
    assert body["overall_score"] == reference.overall_score
    assert [c["status"] for c in body["coverages"]] == [c.status for c in reference.coverages]
    assert reference.capsule_count == body["capsule_count"]


@pytest.mark.parametrize("route", ["coverage", "bundle"])
def test_store_wide_routes_scan_the_servers_capsule_store(
    stack: tuple[TestClient, Path], route: str
) -> None:
    client, store = stack
    _capture(store)
    _capture(store)
    if route == "coverage":
        resp = client.get(
            "/api/compliance/audit/coverage", params={**AUTH, "profile": "eu-ai-act-high-risk"},
            headers=HOST,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["capsule_count"] == 2
    else:
        import base64
        import io
        import json
        import zipfile

        resp = client.post(
            "/api/compliance/audit/bundle", params=AUTH, headers=HOST,
            json={"profile": "eu-ai-act-high-risk"},
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        blob = next(v for k, v in payload.items() if isinstance(v, str) and len(v) > 100)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(blob))) as zf:
            report = json.loads(zf.read("audit-report.json"))
        assert report["capsule_count"] == 2
