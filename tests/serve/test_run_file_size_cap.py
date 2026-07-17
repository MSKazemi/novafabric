"""Bounded-memory run-file serving (v0.61 audit, Wave 2).

`/api/runs/{run_id}/file/{path}` used to `read_text()` / `load_jsonl()` the
whole artifact per request — a multi-GB output file could OOM the server.
Responses are now capped (`NOVA_SERVE_MAX_FILE_BYTES`, default 5 MB): only
the first cap-bytes are read from disk, and oversized responses carry
`truncated: true` plus the real `size_bytes` so the client can say so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "testtoken"
P = {"token": TOKEN}
H = {"host": "127.0.0.1:4321"}


def _write_capsule(base: Path, run_id: str) -> Path:
    cdir = base / run_id
    cdir.mkdir(parents=True)
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.30.0",
        "run_id": run_id,
        "created_at": "2026-05-01T10:00:00+00:00",
        "finished_at": "2026-05-01T10:00:05+00:00",
        "duration_ms": 5000,
        "command": ["python", "agent.py"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    return cdir


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    monkeypatch.setenv("NOVA_SERVE_MAX_FILE_BYTES", "1000")
    base = tmp_path / "capsules"
    base.mkdir()
    cdir = _write_capsule(base, "run_cap_test")
    app = create_app(
        token=TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    return TestClient(app), cdir


def test_small_text_file_served_in_full(setup: tuple[TestClient, Path]) -> None:
    client, cdir = setup
    (cdir / "output.txt").write_text("hello output\n")
    r = client.get("/api/runs/run_cap_test/file/output.txt", params=P, headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["content"] == "hello output\n"
    assert data.get("truncated") is not True


def test_oversized_text_file_is_truncated_not_loaded(
    setup: tuple[TestClient, Path],
) -> None:
    client, cdir = setup
    big = "x" * 10_000
    (cdir / "output.txt").write_text(big)
    r = client.get("/api/runs/run_cap_test/file/output.txt", params=P, headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["truncated"] is True
    assert data["size_bytes"] == 10_000
    assert len(data["content"]) <= 1000


def test_oversized_jsonl_returns_prefix_lines_with_flag(
    setup: tuple[TestClient, Path],
) -> None:
    client, cdir = setup
    line = json.dumps({"model": "m", "pad": "p" * 80})
    (cdir / "model-calls.jsonl").write_text((line + "\n") * 200)
    r = client.get(
        "/api/runs/run_cap_test/file/model-calls.jsonl", params=P, headers=H
    )
    assert r.status_code == 200
    data = r.json()
    assert data["truncated"] is True
    assert 0 < len(data["lines"]) < 200
    assert data["lines"][0]["model"] == "m"


def test_small_jsonl_unchanged_contract(setup: tuple[TestClient, Path]) -> None:
    client, cdir = setup
    (cdir / "tool-calls.jsonl").write_text(json.dumps({"tool": "t"}) + "\n")
    r = client.get("/api/runs/run_cap_test/file/tool-calls.jsonl", params=P, headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["lines"] == [{"tool": "t"}]
    assert data.get("truncated") is not True
