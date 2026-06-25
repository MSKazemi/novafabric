"""Tests for topology auto-seed on startup and periodic re-seed loop.

Covers:
  - auto_seed_skips_when_topology_disabled: loop/seed hooks not registered
  - topology_seeded_on_startup: store is populated before first request
  - reseed_loop_skips_already_seeded_dirs: second manual seed is idempotent
  - seed_respects_only_dirs_filter: non-capsule dirs are ignored
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

# Mark the entire module to require fastapi / starlette
fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-topo-autoseed"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_capsule(
    base: Path,
    run_id: str,
    exit_code: int = 0,
    model_calls: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a minimal capsule directory under *base*."""
    cap = base / run_id
    cap.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "exit_code": exit_code,
        "status": "success" if exit_code == 0 else "failed",
        "created_at": "2026-05-18T10:00:00+00:00",
    }
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    if model_calls:
        lines = "\n".join(json.dumps(mc) for mc in model_calls)
        (cap / "model-calls.jsonl").write_text(lines)
    return cap


# ---------------------------------------------------------------------------
# Test 1: auto-seed skips when topology is disabled
# ---------------------------------------------------------------------------


def test_auto_seed_skips_when_topology_disabled(tmp_path: Path) -> None:
    """When topology_enabled=False, the topology hooks are not populated and
    no seed is attempted on startup — the /api/topology/seed endpoint is absent."""
    capsule_dir = tmp_path / "capsules"
    capsule_dir.mkdir()
    _write_capsule(capsule_dir, "run-001")

    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        topology_enabled=False,
    )

    # Verify that the topology seed route was not registered.
    route_paths = [r.path for r in app.routes if hasattr(r, "path")]  # type: ignore[attr-defined]
    assert "/api/topology/seed" not in route_paths, (
        "/api/topology/seed should not exist when topology is disabled"
    )

    # The _topo_hooks dict should remain un-wired (seed_fn and loop_fn are None).
    # We confirm indirectly: the topology snapshot endpoint should also be absent.
    assert "/api/topology/snapshot" not in route_paths, (
        "/api/topology/snapshot should not exist when topology is disabled"
    )


# ---------------------------------------------------------------------------
# Test 2: topology seeded on startup when enabled
# ---------------------------------------------------------------------------


def test_topology_seeded_on_startup(tmp_path: Path) -> None:
    """With topology_enabled=True and capsules on disk, startup auto-seed
    populates the topology store so the graph is non-empty immediately."""
    capsule_dir = tmp_path / "capsules"
    capsule_dir.mkdir()
    _write_capsule(
        capsule_dir,
        "run-001",
        model_calls=[
            {
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.response.model": "gpt-4o",
                "gen_ai.system": "openai",
            }
        ],
    )
    _write_capsule(capsule_dir, "run-002", exit_code=1)

    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        topology_enabled=True,
    )
    with TestClient(app) as client:
        r = client.get(f"/api/topology/snapshot?{TOKEN_Q}", headers=H)
        assert r.status_code == 200, r.text
        data = r.json()
        # 2 run nodes + 1 model node = 3 nodes seeded at startup
        assert data["node_count"] >= 2, f"Expected ≥2 nodes after startup seed, got {data}"
        # At least 1 edge from run-001 → gpt-4o
        assert data["edge_count"] >= 1, f"Expected ≥1 edges after startup seed, got {data}"


# ---------------------------------------------------------------------------
# Test 3: already-seeded dirs are skipped on subsequent reseed calls
# ---------------------------------------------------------------------------


def test_reseed_loop_skips_already_seeded_dirs(tmp_path: Path) -> None:
    """The reseed loop only processes directories not yet in _topology_seeded_dirs.
    After a full seed, a second manual seed adds nothing new to node count."""
    capsule_dir = tmp_path / "capsules"
    capsule_dir.mkdir()
    _write_capsule(capsule_dir, "run-alpha")

    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        topology_enabled=True,
    )

    with TestClient(app) as client:
        # After startup seed, query node count.
        r1 = client.get(f"/api/topology/snapshot?{TOKEN_Q}", headers=H)
        assert r1.status_code == 200
        count_after_startup = r1.json()["node_count"]
        assert count_after_startup >= 1, "Startup seed should produce at least 1 node"

        # Re-call the seed endpoint (same dirs already tracked) — idempotent.
        r2 = client.post(f"/api/topology/seed?{TOKEN_Q}", headers=H)
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["ok"] is True

        # Node count must be stable (no duplicates from re-seeding).
        r3 = client.get(f"/api/topology/snapshot?{TOKEN_Q}", headers=H)
        assert r3.status_code == 200
        assert r3.json()["node_count"] == count_after_startup, (
            "Re-seeding the same dirs should not increase node count"
        )


# ---------------------------------------------------------------------------
# Test 4: non-capsule directories are ignored by seed
# ---------------------------------------------------------------------------


def test_seed_ignores_dirs_without_manifest(tmp_path: Path) -> None:
    """Directories without a capsule.yaml must not produce topology nodes."""
    capsule_dir = tmp_path / "capsules"
    capsule_dir.mkdir()
    _write_capsule(capsule_dir, "run-x")
    # A directory without capsule.yaml should be silently ignored.
    (capsule_dir / "not-a-capsule").mkdir()
    # A plain file should also be skipped.
    (capsule_dir / "stray.txt").write_text("ignored")

    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        topology_enabled=True,
    )
    with TestClient(app) as client:
        r = client.get(f"/api/topology/snapshot?{TOKEN_Q}", headers=H)
        assert r.status_code == 200
        data = r.json()
        # Only 1 valid capsule dir → 1 run node; not-a-capsule must not appear.
        assert data["node_count"] == 1, (
            f"Expected exactly 1 node (only valid capsule), got {data}"
        )
