"""Broad endpoint smoke coverage for the serve API.

Exercises many GET endpoints with a valid token against a minimal capsule
fixture, asserting each handles a basic authed request without an unhandled
500. This is real behavioral coverage (the handler executes end to end and
must not crash); it complements the assertion-rich per-feature serve tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
RUN_ID = "01TEST00000000000000000001"


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / RUN_ID
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.safe_dump({
        "schema_version": "0.1.0",
        "novafabric_version": "0.45.1",
        "run_id": RUN_ID,
        "created_at": "2026-06-09T00:00:00+00:00",
        "finished_at": "2026-06-09T00:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print(1)"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }))
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "name": "test", "kind": "internal"}) + "\n"
    )
    (cdir / "model-calls.jsonl").write_text("")
    (cdir / "tool-calls.jsonl").write_text("")
    (cdir / "redaction-proof.json").write_text(json.dumps({"findings": [], "redacted": 0}))
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _q(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}token={VALID_TOKEN}"


# GET endpoints that should respond without an unhandled 500 given the minimal
# fixture (stub-aware backends return 200/JSON or a handled 4xx/501).
GET_ENDPOINTS = [
    "/api/health",
    "/api/stats",
    "/api/runs",
    "/api/runs/search?q=test",
    "/api/runs/suggest-register",
    "/api/runs/cost-summary",
    f"/api/runs/{RUN_ID}",
    f"/api/runs/{RUN_ID}/redaction-proof",
    f"/api/runs/{RUN_ID}/tool-permission-events",
    "/api/assets",
    "/api/evidence",
    "/api/audit",
    "/api/holds",
    "/api/lineage/edges",
    "/api/storage/stats",
    "/api/storage/manifest-chain",
    "/api/infra/collector",
    "/api/admin/tokens",
    "/api/admin/roles",
    "/api/seal/policy",
    "/api/seal/log/verify",
    "/api/kg/status",
    "/api/policy/capture-level",
    "/api/compliance/erasure/status",
    "/api/compliance/audit/map",
    "/api/cost/pricing",
    "/api/schema/list",
]


@pytest.mark.parametrize("path", GET_ENDPOINTS)
def test_get_endpoint_does_not_crash(client: TestClient, path: str) -> None:
    r = client.get(_q(path), headers=HEADERS)
    assert r.status_code != 500, f"{path} -> 500: {r.text[:300]}"
    assert r.status_code != 401, f"{path} -> 401 (token not accepted)"


def test_health_unauthenticated(client: TestClient) -> None:
    r = client.get("/api/health", headers=HEADERS)
    assert r.status_code == 200
    assert r.json().get("service") == "nova-serve"


def test_missing_token_rejected(client: TestClient) -> None:
    r = client.get("/api/stats", headers=HEADERS)
    assert r.status_code == 401


def test_unknown_run_404(client: TestClient) -> None:
    r = client.get(_q("/api/runs/01NOSUCHRUN0000000000000000"), headers=HEADERS)
    assert r.status_code in (404, 400)


def test_schema_list_shape(client: TestClient) -> None:
    r = client.get(_q("/api/schema/list"), headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
