"""P8 safe mutation — POST /api/admin/reindex-runs (dashboard safe-mutations policy).

Rebuilds the runs_cache index from the capsule filesystem. Idempotent and
lossless (INSERT-OR-REPLACE per capsule; never deletes). Confirm-gated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


def _write_capsule(capsule_dir: Path, run_id: str) -> None:
    cdir = capsule_dir / run_id
    cdir.mkdir(parents=True)
    (cdir / "capsule.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1.0",
                "novafabric_version": "0.63.0",
                "run_id": run_id,
                "created_at": "2026-07-24T10:00:00+00:00",
                "status": "success",
                "command": ["echo", "hi"],
            }
        )
    )


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c, capsule_dir


def test_requires_token(ctx: tuple[TestClient, Path]) -> None:
    c, _ = ctx
    assert c.post("/api/admin/reindex-runs", json={"confirmed": True}, headers=HEADERS).status_code == 401


def test_requires_confirmation(ctx: tuple[TestClient, Path]) -> None:
    c, _ = ctx
    r = c.post(f"/api/admin/reindex-runs?{TOKEN_Q}", json={}, headers=HEADERS)
    assert r.status_code == 400


def test_reindexes_capsules(ctx: tuple[TestClient, Path]) -> None:
    c, capsule_dir = ctx
    _write_capsule(capsule_dir, "run-a")
    _write_capsule(capsule_dir, "run-b")
    r = c.post(f"/api/admin/reindex-runs?{TOKEN_Q}", json={"confirmed": True}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["reindexed"] == 2
    assert body["total"] == 2


def test_is_idempotent(ctx: tuple[TestClient, Path]) -> None:
    c, capsule_dir = ctx
    _write_capsule(capsule_dir, "run-a")
    first = c.post(f"/api/admin/reindex-runs?{TOKEN_Q}", json={"confirmed": True}, headers=HEADERS).json()
    second = c.post(f"/api/admin/reindex-runs?{TOKEN_Q}", json={"confirmed": True}, headers=HEADERS).json()
    # Re-running does not lose or duplicate rows (INSERT-OR-REPLACE, full rebuild).
    assert first["total"] == second["total"] == 1
