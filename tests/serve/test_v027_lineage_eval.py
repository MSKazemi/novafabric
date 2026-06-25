"""Tests for v0.27.0 dashboard parity endpoints.

Covers:
  POST /api/lineage/import  — import OpenLineage events from a capsule directory
  POST /api/eval/compare    — compare two EvalResult JSON objects for regressions
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-v027"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_capsule(capsule_dir: Path, run_id: str) -> Path:
    """Create a minimal valid capsule sub-directory (for lineage import tests)."""
    cap = capsule_dir / run_id
    cap.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "status": "success",
        "model_call_count": 0,
        "tool_call_count": 0,
        "duration_ms": 1000,
        "capture_mode": "direct",
        "novafabric_version": "0.27.0",
    }
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    return cap


def _make_eval_result_json(
    suite_id: str = "novafabric-smoke-v1",
    accuracy: float = 0.90,
) -> str:
    """Return a valid EvalResult JSON string."""
    return json.dumps({
        "schema_version": "0.1.0",
        "suite_id": suite_id,
        "suite_version": "1.0.0",
        "oci_digest": "",
        "run_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "capsule_id": "run_test_000",
        "passed": accuracy >= 0.80,
        "metrics": [
            {
                "name": "accuracy",
                "value": accuracy,
                "unit": "ratio",
                "threshold": 0.80,
                "passed": accuracy >= 0.80,
            }
        ],
    })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
# POST /api/lineage/import
# ---------------------------------------------------------------------------

class TestLineageImportEndpoint:
    def test_missing_capsule_path_returns_422(self, client: TestClient) -> None:
        r = client.post(
            f"/api/lineage/import?{TOKEN_Q}",
            headers=H,
            json={},
        )
        assert r.status_code == 422
        assert "capsule_path" in r.json()["detail"]

    def test_nonexistent_path_returns_404(self, client: TestClient) -> None:
        r = client.post(
            f"/api/lineage/import?{TOKEN_Q}",
            headers=H,
            json={"capsule_path": "/tmp/definitely_does_not_exist_abc123"},
        )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_valid_capsule_dir_with_empty_capsules_returns_ok(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        """Capsule dir exists and contains a sub-capsule; no lineage events — should succeed."""
        _make_capsule(capsule_dir, "01TESTIMPORT")
        r = client.post(
            f"/api/lineage/import?{TOKEN_Q}",
            headers=H,
            json={"capsule_path": str(capsule_dir)},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "imported" in data
        assert "skipped" in data
        assert "file_count" in data
        assert "note" in data

    def test_bare_run_id_resolves_to_capsule_dir(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        """A bare run_id (no slashes) should be resolved under capsule_dir."""
        run_id = "01TESTBAREID"
        _make_capsule(capsule_dir, run_id)
        r = client.post(
            f"/api/lineage/import?{TOKEN_Q}",
            headers=H,
            json={"capsule_path": run_id},
        )
        # The bare run_id resolves to capsule_dir / run_id which is a valid dir,
        # but it has no capsule sub-dirs so file_count will be 0.
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True

    def test_requires_auth(self, client: TestClient) -> None:
        r = client.post("/api/lineage/import", headers=H, json={"capsule_path": "/tmp"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/eval/compare
# ---------------------------------------------------------------------------

class TestEvalCompareEndpoint:
    def test_missing_baseline_json_returns_422(self, client: TestClient) -> None:
        r = client.post(
            f"/api/eval/compare?{TOKEN_Q}",
            headers=H,
            json={"candidate_json": _make_eval_result_json()},
        )
        assert r.status_code == 422
        assert "baseline_json" in r.json()["detail"]

    def test_missing_candidate_json_returns_422(self, client: TestClient) -> None:
        r = client.post(
            f"/api/eval/compare?{TOKEN_Q}",
            headers=H,
            json={"baseline_json": _make_eval_result_json()},
        )
        assert r.status_code == 422
        assert "candidate_json" in r.json()["detail"]

    def test_valid_comparison_returns_ok(self, client: TestClient) -> None:
        baseline = _make_eval_result_json(accuracy=0.90)
        candidate = _make_eval_result_json(accuracy=0.85)
        r = client.post(
            f"/api/eval/compare?{TOKEN_Q}",
            headers=H,
            json={"baseline_json": baseline, "candidate_json": candidate},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "regression_detected" in data
        assert "metrics" in data
        assert isinstance(data["metrics"], list)
        assert len(data["metrics"]) == 1
        assert data["metrics"][0]["name"] == "accuracy"

    def test_regression_detection_on_drop(self, client: TestClient) -> None:
        """A large accuracy drop should be flagged as significant when using alpha threshold."""
        baseline = _make_eval_result_json(accuracy=0.95)
        candidate = _make_eval_result_json(accuracy=0.50)
        r = client.post(
            f"/api/eval/compare?{TOKEN_Q}",
            headers=H,
            json={"baseline_json": baseline, "candidate_json": candidate, "alpha": 0.05},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["regression_detected"] is True
        metric = data["metrics"][0]
        assert metric["delta"] < 0
        assert metric["significant"] is True

    def test_no_regression_on_equal_values(self, client: TestClient) -> None:
        baseline = _make_eval_result_json(accuracy=0.90)
        candidate = _make_eval_result_json(accuracy=0.90)
        r = client.post(
            f"/api/eval/compare?{TOKEN_Q}",
            headers=H,
            json={"baseline_json": baseline, "candidate_json": candidate},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["regression_detected"] is False

    def test_requires_auth(self, client: TestClient) -> None:
        baseline = _make_eval_result_json()
        candidate = _make_eval_result_json()
        r = client.post(
            "/api/eval/compare",
            headers=H,
            json={"baseline_json": baseline, "candidate_json": candidate},
        )
        assert r.status_code == 401
