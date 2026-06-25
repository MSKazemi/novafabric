"""Tests for POST /api/runs/{run_id}/validate (DC-7 — Capsule Validation UI)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

VALID_TOKEN = "validate-test-token-dc7-1234"
HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def client(capsule_dir: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=None,
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _make_real_capsule(capsule_dir: Path) -> str:
    """Create a genuine capsule via CaptureOrchestrator; returns run_id."""
    from novafabric.capture.orchestrator import CaptureOrchestrator

    orch = CaptureOrchestrator(base_dir=capsule_dir)
    result = orch.run(command=[sys.executable, "-c", "pass"])
    manifest = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())
    return manifest["run_id"]


def _make_broken_capsule(capsule_dir: Path, run_id: str) -> Path:
    """Create a minimal capsule missing required files — guaranteed to be invalid."""
    cdir = capsule_dir / run_id
    cdir.mkdir()
    # Write only capsule.yaml with insufficient fields so schema validation also fails
    # AND omit all required JSONL files and subdirectories.
    (cdir / "capsule.yaml").write_text(yaml.safe_dump({
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": "success",
    }))
    return cdir


# ---------- 1. Valid capsule → 200 valid=true ----------

def test_validate_valid_capsule(client: TestClient, capsule_dir: Path) -> None:
    """A real capsule produced by CaptureOrchestrator should validate cleanly."""
    run_id = _make_real_capsule(capsule_dir)
    res = client.post(
        f"/api/runs/{run_id}/validate?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["run_id"] == run_id
    assert body["valid"] is True
    assert body["errors"] == []


# ---------- 2. Broken capsule → 200 valid=false with errors ----------

def test_validate_broken_capsule(client: TestClient, capsule_dir: Path) -> None:
    """A capsule with missing files and a bad manifest should return valid=false with errors."""
    run_id = "01BROKEN000000000000000001"
    _make_broken_capsule(capsule_dir, run_id)
    res = client.post(
        f"/api/runs/{run_id}/validate?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is False
    errors = body["errors"]
    assert isinstance(errors, list)
    assert len(errors) > 0
    # Missing JSONL files should be reported
    error_text = " ".join(errors)
    assert "missing" in error_text.lower()


def test_validate_broken_capsule_reports_missing_files(
    client: TestClient, capsule_dir: Path
) -> None:
    """Errors list should mention the missing required files."""
    run_id = "01BROKEN000000000000000002"
    _make_broken_capsule(capsule_dir, run_id)
    res = client.post(
        f"/api/runs/{run_id}/validate?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    body = res.json()
    assert body["valid"] is False
    errors = body["errors"]
    # At minimum trace.jsonl, model-calls.jsonl, tool-calls.jsonl should be missing
    missing_refs = [e for e in errors if "missing" in e.lower()]
    assert len(missing_refs) >= 3


# ---------- 3. Path traversal → 400 ----------

def test_validate_path_traversal_rejected(client: TestClient) -> None:
    """run_id containing '..' must not be allowed.

    The ASGI stack normalises ``/api/runs/../../etc/passwd/validate`` to
    ``/etc/passwd/validate`` before routing, so FastAPI never matches the
    route at all (404) rather than reaching _resolve_capsule's check (400).
    Both 400 and 404 are acceptable rejections.
    """
    res = client.post(
        f"/api/runs/../../etc/passwd/validate?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code in (400, 404)


def test_validate_run_id_with_slash_rejected(client: TestClient) -> None:
    """run_id containing '/' must be rejected with 400."""
    res = client.post(
        "/api/runs/some%2Fpath/validate",
        params={"token": VALID_TOKEN},
        headers=HEADERS,
    )
    # FastAPI URL-decodes the path; the endpoint's _resolve_capsule checks for /
    # After URL-decoding, "some/path" contains "/" so _resolve_capsule returns 400,
    # or FastAPI itself routes it differently. Either way, it must not return 200.
    assert res.status_code in (400, 404)


# ---------- 4. Nonexistent run → 404 ----------

def test_validate_nonexistent_run_returns_404(client: TestClient) -> None:
    """A run_id that doesn't exist in the capsule dir must return 404."""
    res = client.post(
        f"/api/runs/nonexistent-run-id-dc7/validate?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 404


# ---------- 5. Auth guard ----------

def test_validate_requires_token(client: TestClient, capsule_dir: Path) -> None:
    """Calling the validate endpoint without a token must return 401."""
    run_id = "01BROKEN000000000000000003"
    _make_broken_capsule(capsule_dir, run_id)
    res = client.post(
        f"/api/runs/{run_id}/validate",
        headers=HEADERS,
    )
    assert res.status_code == 401


# ---------- 6. Capsule with removed required file → invalid ----------

def test_validate_capsule_missing_trace_reports_error(
    client: TestClient, capsule_dir: Path
) -> None:
    """After deleting trace.jsonl from a valid capsule, validate should report it missing."""
    run_id = _make_real_capsule(capsule_dir)
    # Find the capsule dir by scanning for the run_id
    capsule_subdirs = [d for d in capsule_dir.iterdir() if d.is_dir()]
    target = None
    for d in capsule_subdirs:
        cap_yaml = d / "capsule.yaml"
        if cap_yaml.exists():
            m = yaml.safe_load(cap_yaml.read_text())
            if m.get("run_id") == run_id:
                target = d
                break
    assert target is not None, "Could not find capsule directory"
    (target / "trace.jsonl").unlink()

    res = client.post(
        f"/api/runs/{run_id}/validate?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is False
    assert any("trace.jsonl" in e for e in body["errors"])
