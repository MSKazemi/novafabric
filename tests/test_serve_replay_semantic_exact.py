"""Tests for POST /api/runs/{run_id}/replay/semantic and /exact (DC-4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

VALID_TOKEN = "replay-se-test-token-dc4-1234"
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
    """Create a genuine capsule and return its run_id."""
    from novafabric.capture.orchestrator import CaptureOrchestrator

    orch = CaptureOrchestrator(base_dir=capsule_dir)
    result = orch.run(command=[sys.executable, "-c", "pass"])
    manifest = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())
    return manifest["run_id"]


def _make_capsule_with_model_calls(capsule_dir: Path, run_id: str, model_calls: list[dict]) -> Path:
    """Create a minimal capsule with explicit model-calls.jsonl content."""
    cdir = capsule_dir / run_id
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": "success",
        "created_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "duration_ms": 1000,
        "command": [sys.executable, "-c", "pass"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.9.0",
        "working_directory": "~",
        "host": {
            "os": "linux", "arch": "x86_64", "cpu_count": 1,
            "memory_bytes": 1024, "hostname_redacted": True, "gpu": [],
        },
        "environment_ref": "env.lock",
        "replay_policy_ref": "replay.yaml",
        "redaction_proof_ref": "redaction-proof.json",
        "trace_ref": "trace.jsonl",
        "trace_root_span_id": "a" * 16,
        "model_calls_ref": "model-calls.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "assets_ref": "assets.jsonl",
        "inputs": [],
        "outputs": [],
        "model_call_count": len(model_calls),
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "model-calls.jsonl").write_text(
        "\n".join(json.dumps(mc) for mc in model_calls) + ("\n" if model_calls else "")
    )
    (cdir / "tool-calls.jsonl").write_text("")
    (cdir / "assets.jsonl").write_text("")
    (cdir / "trace.jsonl").write_text("")
    env_lock = {"schema_version": "0.1.0", "mode": "best-effort"}
    (cdir / "env.lock").write_text(yaml.safe_dump(env_lock))
    (cdir / "replay.yaml").write_text(yaml.safe_dump({"schema_version": "0.1.0"}))
    (cdir / "redaction-proof.json").write_text("{}")
    return cdir


def _make_deterministic_capsule(capsule_dir: Path, run_id: str) -> Path:
    """Create a capsule with deterministic env.lock and seeded model calls."""
    # Real records use flat OTel-GenAI keys (gen_ai.request.seed, gen_ai.request.*).
    model_calls = [
        {
            "model_call_id": "01AAAAAAAAAAAAAAAAAAAAAAAA",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.request.seed": 42,
            "gen_ai.request.messages": [{"role": "user", "content": "hello"}],
            "gen_ai.response.choices": [
                {"message": {"role": "assistant", "content": "Hi there!"}}
            ],
        }
    ]
    cdir = _make_capsule_with_model_calls(capsule_dir, run_id, model_calls)
    det_lock = {"schema_version": "0.1.0", "mode": "deterministic"}
    (cdir / "env.lock").write_text(yaml.safe_dump(det_lock))
    return cdir


# ---------- Semantic endpoint ----------

def test_semantic_replay_returns_similarity_score(client: TestClient, capsule_dir: Path) -> None:
    """Semantic endpoint returns a similarity_score in [0.0, 1.0] for a real capsule."""
    run_id = _make_real_capsule(capsule_dir)
    res = client.post(
        f"/api/runs/{run_id}/replay/semantic?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["mode"] == "semantic"
    assert result["status"] == "success"
    assert "replay_id" in result
    assert "similarity_score" in result
    score = result["similarity_score"]
    assert isinstance(score, (int, float))
    assert 0.0 <= score <= 1.0


def test_semantic_replay_with_identical_model_calls(client: TestClient, capsule_dir: Path) -> None:
    """Capsule with two identical model call responses → similarity_score should be 1.0."""
    run_id = "01SEMANTIC00IDENTICAL00001"
    model_calls = [
        {
            "model_call_id": "01AAAAAAAAAAAAAAAAAAAAAAAA",
            "model": "gpt-4o",
            "request": {"messages": [{"role": "user", "content": "hello"}]},
            "response": {"choices": [{"message": {"role": "assistant",
                                                   "content": "Same answer."}}]},
        },
        {
            "model_call_id": "01BBBBBBBBBBBBBBBBBBBBBBBB",
            "model": "gpt-4o",
            "request": {"messages": [{"role": "user", "content": "hello again"}]},
            "response": {"choices": [{"message": {"role": "assistant",
                                                   "content": "Same answer."}}]},
        },
    ]
    _make_capsule_with_model_calls(capsule_dir, run_id, model_calls)
    res = client.post(
        f"/api/runs/{run_id}/replay/semantic?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["similarity_score"] == 1.0
    assert result["matched_run_id"] == run_id


def test_semantic_replay_with_no_model_calls(client: TestClient, capsule_dir: Path) -> None:
    """Capsule with zero model calls → similarity_score defaults to 1.0."""
    run_id = "01SEMANTIC00NOMODELCALLS01"
    _make_capsule_with_model_calls(capsule_dir, run_id, [])
    res = client.post(
        f"/api/runs/{run_id}/replay/semantic?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["similarity_score"] == 1.0


def test_semantic_replay_unconfirmed_returns_400(client: TestClient, capsule_dir: Path) -> None:
    run_id = _make_real_capsule(capsule_dir)
    res = client.post(
        f"/api/runs/{run_id}/replay/semantic?token={VALID_TOKEN}",
        json={"confirmed": False},
        headers=HEADERS,
    )
    assert res.status_code == 400


def test_semantic_replay_nonexistent_returns_404(client: TestClient) -> None:
    res = client.post(
        f"/api/runs/nonexistent-run-dc4-sem/replay/semantic?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 404


def test_semantic_replay_requires_token(client: TestClient, capsule_dir: Path) -> None:
    run_id = _make_real_capsule(capsule_dir)
    res = client.post(
        f"/api/runs/{run_id}/replay/semantic",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 401


# ---------- Exact endpoint ----------

def test_exact_replay_eligible_capsule(client: TestClient, capsule_dir: Path) -> None:
    """Capsule with deterministic env.lock and seeded model call → exact_eligible=True."""
    run_id = "01EXACT000ELIGIBLE00000001"
    _make_deterministic_capsule(capsule_dir, run_id)
    res = client.post(
        f"/api/runs/{run_id}/replay/exact?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["mode"] == "exact"
    assert result["status"] == "success"
    assert result["exact_eligible"] is True
    assert result["exact_reasons"] == []
    assert result["exact_hash_count"] == 1


def test_exact_replay_not_eligible_best_effort_env(client: TestClient, capsule_dir: Path) -> None:
    """Capsule with best-effort env.lock → exact_eligible=False with reason."""
    run_id = "01EXACT000NOTELIGIBLE00001"
    model_calls = [
        {
            "model_call_id": "01CCCCCCCCCCCCCCCCCCCCCCCC",
            "model": "gpt-4o",
            "seed": 42,
            "request": {"messages": [{"role": "user", "content": "hi"}]},
            "response": {"choices": [{"message": {"role": "assistant", "content": "hello"}}]},
        }
    ]
    _make_capsule_with_model_calls(capsule_dir, run_id, model_calls)
    # env.lock already has mode=best-effort from _make_capsule_with_model_calls
    res = client.post(
        f"/api/runs/{run_id}/replay/exact?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["exact_eligible"] is False
    reasons = result["exact_reasons"]
    assert len(reasons) >= 1
    assert any("deterministic" in r for r in reasons)


def test_exact_replay_not_eligible_no_seed(client: TestClient, capsule_dir: Path) -> None:
    """Capsule with deterministic env but unseeded model call → exact_eligible=False."""
    run_id = "01EXACT000NOSEED000000001"
    model_calls = [
        {
            "model_call_id": "01DDDDDDDDDDDDDDDDDDDDDDDD",
            "model": "gpt-4o",
            # no seed field
            "request": {"messages": [{"role": "user", "content": "hi"}]},
            "response": {"choices": [{"message": {"role": "assistant", "content": "hello"}}]},
        }
    ]
    cdir = _make_capsule_with_model_calls(capsule_dir, run_id, model_calls)
    det_lock2 = {"schema_version": "0.1.0", "mode": "deterministic"}
    (cdir / "env.lock").write_text(yaml.safe_dump(det_lock2))
    res = client.post(
        f"/api/runs/{run_id}/replay/exact?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["exact_eligible"] is False
    reasons = result["exact_reasons"]
    assert any("seed" in r.lower() for r in reasons)


def test_exact_replay_not_eligible_legacy_env_lock(client: TestClient, capsule_dir: Path) -> None:
    """Old capsules without a 'mode' field → treated as best-effort, not 'missing'."""
    run_id = "01EXACT000LEGACY000000001"
    model_calls = [
        {
            "model_call_id": "01EEEEEEEEEEEEEEEEEEEEEEEE",
            "model": "gpt-4o",
            "seed": 42,
            "request": {"messages": [{"role": "user", "content": "hi"}]},
            "response": {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        }
    ]
    cdir = _make_capsule_with_model_calls(capsule_dir, run_id, model_calls)
    # Simulate old pre-v0.2 capsule: no 'mode' field, flat host.python layout
    legacy_lock = {
        "schema_version": "0.1.0",
        "host": {"arch": "x86_64", "os": "Linux 6.14.0", "python": "3.12.4",
                 "cpu_count": 16, "memory_bytes": 67108864000},
        "runtime": {"novafabric": "0.6.12", "openai": "1.55.0"},
        "locale": {"LANG": "C.UTF-8", "TZ": "UTC"},
        "captured_at": "2026-04-15T10:00:00+00:00",
    }
    (cdir / "env.lock").write_text(yaml.safe_dump(legacy_lock))
    res = client.post(
        f"/api/runs/{run_id}/replay/exact?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["exact_eligible"] is False
    reasons = result["exact_reasons"]
    assert any("best-effort" in r for r in reasons), f"expected best-effort in reasons: {reasons}"
    assert not any("missing" in r for r in reasons), f"should not say 'missing': {reasons}"


def test_exact_replay_unconfirmed_returns_400(client: TestClient, capsule_dir: Path) -> None:
    run_id = _make_real_capsule(capsule_dir)
    res = client.post(
        f"/api/runs/{run_id}/replay/exact?token={VALID_TOKEN}",
        json={"confirmed": False},
        headers=HEADERS,
    )
    assert res.status_code == 400


def test_exact_replay_nonexistent_returns_404(client: TestClient) -> None:
    res = client.post(
        f"/api/runs/nonexistent-run-dc4-exact/replay/exact?token={VALID_TOKEN}",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 404


def test_exact_replay_requires_token(client: TestClient, capsule_dir: Path) -> None:
    run_id = _make_real_capsule(capsule_dir)
    res = client.post(
        f"/api/runs/{run_id}/replay/exact",
        json={"confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 401
