"""P8 safe mutation — POST /api/admin/reindex-runs (dashboard safe-mutations policy).

Rebuilds the runs_cache index from the capsule filesystem. Idempotent and
lossless (INSERT-OR-REPLACE per capsule; never deletes). Confirm-gated.
"""

from __future__ import annotations

import shutil
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


# ---------------------------------------------------------------------------
# Orphan pruning (opt-in): a rebuild is additive, so a row whose capsule
# directory has disappeared survives forever and 404s on every drill-in.
# ---------------------------------------------------------------------------


class TestOrphanPruning:
    def test_default_run_keeps_orphans(self, ctx: tuple[TestClient, Path]) -> None:
        """Pruning is opt-in — a plain reindex must not delete index rows."""
        client, capsule_dir = ctx
        _write_capsule(capsule_dir, "run-keep")
        _write_capsule(capsule_dir, "run-vanishes")
        client.post(
            f"/api/admin/reindex-runs?{TOKEN_Q}", headers=HEADERS, json={"confirmed": True}
        )

        # The capsule disappears (moved store / deleted run), index row remains.
        shutil.rmtree(capsule_dir / "run-vanishes")

        resp = client.post(
            f"/api/admin/reindex-runs?{TOKEN_Q}", headers=HEADERS, json={"confirmed": True}
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["pruned"] == 0
        assert body["total"] == 2  # orphan still listed

    def test_prune_removes_only_the_orphan(self, ctx: tuple[TestClient, Path]) -> None:
        client, capsule_dir = ctx
        _write_capsule(capsule_dir, "run-keep")
        _write_capsule(capsule_dir, "run-vanishes")
        client.post(
            f"/api/admin/reindex-runs?{TOKEN_Q}", headers=HEADERS, json={"confirmed": True}
        )
        shutil.rmtree(capsule_dir / "run-vanishes")

        resp = client.post(
            f"/api/admin/reindex-runs?{TOKEN_Q}",
            headers=HEADERS,
            json={"confirmed": True, "prune": True},
        )
        body = resp.json()
        assert body["pruned"] == 1
        assert body["total"] == 1

        listed = client.get(f"/api/runs?{TOKEN_Q}", headers=HEADERS).json()
        ids = {r["run_id"] for r in listed["runs"]}
        assert ids == {"run-keep"}, "surviving capsule must stay indexed"

    def test_prune_is_idempotent(self, ctx: tuple[TestClient, Path]) -> None:
        client, capsule_dir = ctx
        _write_capsule(capsule_dir, "run-keep")
        payload = {"confirmed": True, "prune": True}
        client.post(f"/api/admin/reindex-runs?{TOKEN_Q}", headers=HEADERS, json=payload)
        second = client.post(
            f"/api/admin/reindex-runs?{TOKEN_Q}", headers=HEADERS, json=payload
        ).json()
        assert second["pruned"] == 0
        assert second["total"] == 1

    def test_prune_never_deletes_a_capsule(self, ctx: tuple[TestClient, Path]) -> None:
        """Only the derived index row is removed — capsules are evidence."""
        client, capsule_dir = ctx
        _write_capsule(capsule_dir, "run-keep")
        client.post(
            f"/api/admin/reindex-runs?{TOKEN_Q}",
            headers=HEADERS,
            json={"confirmed": True, "prune": True},
        )
        assert (capsule_dir / "run-keep" / "capsule.yaml").is_file()
