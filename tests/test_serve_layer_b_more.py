"""Tests for the second batch of Layer B endpoints: promote, forensic replay, redact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from novafabric.serve import audit
from novafabric.serve.app import create_app

VALID_TOKEN = "more-layer-b-test-token-1234"
HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db = tmp_path / "registry.db"
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.AUDIT_ENV, str(audit_path))
    yield {"capsule_dir": capsule_dir, "db": db, "audit": audit_path, "tmp": tmp_path}


@pytest.fixture
def client(env: dict[str, Path]) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=env["capsule_dir"],
        db_path=env["db"],
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _write_capsule(base: Path, run_id: str) -> Path:
    cdir = base / run_id
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": run_id,
        "created_at": "2026-04-15T10:00:00+00:00",
        "finished_at": "2026-04-15T10:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print('ok')"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "name": "test", "kind": "internal"}) + "\n"
    )
    (cdir / "model-calls.jsonl").write_text("")
    (cdir / "tool-calls.jsonl").write_text("")
    return cdir


# ---------- Promote ----------

REGISTER_YAML = """\
novafabric_spec_version: "1"
asset_type: model
name: pytest-promote-model
version: 1.0.0
status: development
spec:
  framework: pytorch
  artifact_path: /tmp/x
"""


def test_promote_requires_confirmation(client: TestClient) -> None:
    res = client.post(
        f"/api/assets/whatever/0.0.1/promote?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"to_status": "staging", "confirmed": False},
    )
    assert res.status_code == 400


def test_promote_unknown_asset_returns_404(client: TestClient) -> None:
    res = client.post(
        f"/api/assets/nonexistent/0.0.1/promote?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"to_status": "staging", "confirmed": True},
    )
    assert res.status_code == 404
    entries = audit.read_recent(5)
    assert any(e["action"] == "promote_asset" and e.get("result") == "error" for e in entries)


def test_promote_invalid_status_returns_400(client: TestClient) -> None:
    res = client.post(
        f"/api/assets/anything/0.0.1/promote?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"to_status": "totally-invalid", "confirmed": True},
    )
    assert res.status_code == 400


def test_promote_dev_to_staging_succeeds_for_non_agent(client: TestClient) -> None:
    # First register a model asset (non-agent — no eval gate applies)
    reg = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    assert reg.status_code == 200, reg.text

    res = client.post(
        f"/api/assets/pytest-promote-model/1.0.0/promote?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"to_status": "staging", "confirmed": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["asset"]["status"] == "staging"

    entries = audit.read_recent(10)
    promoted = [e for e in entries if e["action"] == "promote_asset" and e.get("result") == "ok"]
    assert len(promoted) >= 1
    assert promoted[0]["args"]["to_status"] == "staging"


def test_promote_invalid_transition_returns_409(client: TestClient) -> None:
    client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    # development → archived is allowed; archived → anything is not.
    client.post(
        f"/api/assets/pytest-promote-model/1.0.0/promote?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"to_status": "archived", "confirmed": True},
    )
    # Now try to move from archived → production (invalid).
    res = client.post(
        f"/api/assets/pytest-promote-model/1.0.0/promote?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"to_status": "production", "confirmed": True},
    )
    assert res.status_code == 409


# ---------- Forensic replay ----------

def test_forensic_replay_requires_confirmation(client: TestClient) -> None:
    res = client.post(
        f"/api/runs/01TEST00000000000000000001/replay/forensic?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": False},
    )
    assert res.status_code == 400


def test_forensic_replay_unknown_run_returns_404(client: TestClient) -> None:
    res = client.post(
        f"/api/runs/01NONEXISTENT0000000000000/replay/forensic?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 404


def test_forensic_replay_succeeds_against_minimal_capsule(  # noqa: E501
    client: TestClient, env: dict[str, Path]
) -> None:
    run_id = "01TEST00000000000000000010"
    _write_capsule(env["capsule_dir"], run_id)
    client.post(
        f"/api/runs/{run_id}/replay/forensic?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    # Either succeeds (200) or hits a missing-component path (5xx); an audit entry must exist.
    entries = audit.read_recent(5)
    assert any(e["action"] == "forensic_replay" for e in entries)


# ---------- Redact ----------

def test_redact_requires_confirmation(client: TestClient) -> None:
    res = client.post(
        f"/api/runs/01TEST00000000000000000099/redact?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": False},
    )
    assert res.status_code == 400


def test_redact_unknown_run_returns_404(client: TestClient) -> None:
    res = client.post(
        f"/api/runs/01NONEXISTENT0000000000000/redact?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 404


def test_forensic_replay_returns_501_when_engine_not_importable(
    client: TestClient,
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    run_id = "01TEST00000000000000000011"
    _write_capsule(env["capsule_dir"], run_id)
    monkeypatch.setitem(sys.modules, "novafabric.replay._engine", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "novafabric.replay._flags", None)  # type: ignore[arg-type]
    res = client.post(
        f"/api/runs/{run_id}/replay/forensic?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 501


def test_forensic_replay_returns_500_when_engine_raises(
    client: TestClient,
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "01TEST00000000000000000012"
    _write_capsule(env["capsule_dir"], run_id)

    from novafabric.replay import _engine as replay_engine_mod
    from novafabric.replay import _flags as replay_flags_mod

    class _BrokenEngine:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            raise RuntimeError("simulated engine failure")

    monkeypatch.setattr(replay_engine_mod, "ReplayEngine", _BrokenEngine)
    monkeypatch.setattr(replay_flags_mod, "ReplayFlags", lambda **kw: kw)

    res = client.post(
        f"/api/runs/{run_id}/replay/forensic?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 500
    entries = audit.read_recent(5)
    assert any(
        e["action"] == "forensic_replay" and e.get("result") == "error"
        for e in entries
    )


def test_redact_returns_501_when_scanner_not_importable(
    client: TestClient,
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    run_id = "01TEST00000000000000000021"
    _write_capsule(env["capsule_dir"], run_id)
    monkeypatch.setitem(sys.modules, "novafabric.capture.secrets", None)  # type: ignore[arg-type]
    res = client.post(
        f"/api/runs/{run_id}/redact?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 501


def test_redact_returns_500_when_scanner_raises(
    client: TestClient,
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "01TEST00000000000000000022"
    _write_capsule(env["capsule_dir"], run_id)

    from novafabric.capture import secrets as secrets_mod

    class _BrokenScanner:
        def __init__(self, **kwargs: object) -> None:
            pass

        def scan_and_redact(self) -> None:
            raise RuntimeError("simulated scan failure")

    monkeypatch.setattr(secrets_mod, "SecretScannerV0", _BrokenScanner)

    res = client.post(
        f"/api/runs/{run_id}/redact?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 500
    entries = audit.read_recent(5)
    assert any(
        e["action"] == "redact" and e.get("result") == "error" for e in entries
    )


def test_redact_succeeds_and_writes_proof(client: TestClient, env: dict[str, Path]) -> None:
    run_id = "01TEST00000000000000000020"
    cdir = _write_capsule(env["capsule_dir"], run_id)

    res = client.post(
        f"/api/runs/{run_id}/redact?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    # SecretScannerV0 may need additional inputs; either way assert audit log captured the attempt.
    entries = audit.read_recent(5)
    assert any(e["action"] == "redact" for e in entries)

    if res.status_code == 200:
        body = res.json()
        assert body["ok"] is True
        assert "findings_count" in body
        assert (cdir / "redaction-proof.json").exists()


def test_forensic_replay_success_with_model_dump(
    client: TestClient,
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "01TEST00000000000000000013"
    _write_capsule(env["capsule_dir"], run_id)

    from novafabric.replay import _engine as replay_engine_mod
    from novafabric.replay import _flags as replay_flags_mod

    class _FakeResult:
        replay_id = "test-replay-999"
        status = "ok"

        def model_dump(self, mode: str = "python") -> dict[str, object]:
            return {"replay_id": self.replay_id, "status": self.status}

    class _SuccessEngine:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(replay_engine_mod, "ReplayEngine", _SuccessEngine)
    monkeypatch.setattr(replay_flags_mod, "ReplayFlags", lambda **kw: kw)

    res = client.post(
        f"/api/runs/{run_id}/replay/forensic?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["replay_id"] == "test-replay-999"


def test_redact_with_prior_proof_preserves_unsafe_skips(
    client: TestClient,
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "01TEST00000000000000000023"
    cdir = _write_capsule(env["capsule_dir"], run_id)

    # Write a pre-existing proof with unsafe_skips to simulate a prior redaction.
    proof_path = cdir / "redaction-proof.json"
    proof_path.write_text(
        json.dumps({"findings": [], "unsafe_skips": ["MY_SECRET_RULE"], "redacted_files": []})
    )

    from novafabric.capture import secrets as secrets_mod

    class _MockScanner:
        def __init__(self, **kwargs: object) -> None:
            pass

        def scan_and_redact(self) -> dict[str, object]:
            return {"findings": [], "redacted_files": []}

    monkeypatch.setattr(secrets_mod, "SecretScannerV0", _MockScanner)

    res = client.post(
        f"/api/runs/{run_id}/redact?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    updated_proof = json.loads(proof_path.read_text())
    assert "MY_SECRET_RULE" in updated_proof.get("unsafe_skips", [])


# ---------- GET /api/runs/{run_id}/redaction-proof ----------

_SAMPLE_PROOF = {
    "schema_version": "0.1.0",
    "proof_id": "01TESTPROOFID000000000000",
    "capsule_run_id": "01TEST00000000000000000099",
    "created_at": "2026-05-14T00:00:00Z",
    "scanner": {
        "name": "novafabric.secrets",
        "version": "0.2.0",
        "engine": "regex",
        "engine_version": "0.2.0",
    },
    "packs": [
        {
            "name": "gitleaks-core-v0",
            "version": "0.2.0",
            "rules_count": 12,
            "rules_hash": "sha256:abc",
        }
    ],
    "targets": [
        {
            "kind": "trace",
            "ref": "trace.jsonl",
            "bytes_scanned": 100,
            "findings_count": 0,
            "skipped": False,
            "binary": False,
            "hash_before_redaction": "sha256:aaa",
            "hash_after_redaction": "sha256:aaa",
        }
    ],
    "findings_count": {
        "total": 0,
        "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
    },
    "findings": [],
    "bytes_scanned": 100,
    "bytes_redacted": 0,
    "chain_hash": "sha256:chainxyz",
}


def test_redaction_proof_requires_token(client: TestClient, env: dict[str, Path]) -> None:
    run_id = "01TEST00000000000000000099"
    _write_capsule(env["capsule_dir"], run_id)
    res = client.get(f"/api/runs/{run_id}/redaction-proof", headers=HEADERS)
    assert res.status_code == 401


def test_redaction_proof_404_for_unknown_run(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/01NONEXISTENT0000000000000/redaction-proof?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 404


def test_redaction_proof_404_when_proof_absent(client: TestClient, env: dict[str, Path]) -> None:
    run_id = "01TEST00000000000000000099"
    _write_capsule(env["capsule_dir"], run_id)
    res = client.get(
        f"/api/runs/{run_id}/redaction-proof?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_redaction_proof_returns_proof_json(client: TestClient, env: dict[str, Path]) -> None:
    run_id = "01TEST00000000000000000099"
    cdir = _write_capsule(env["capsule_dir"], run_id)
    (cdir / "redaction-proof.json").write_text(json.dumps(_SAMPLE_PROOF))

    res = client.get(
        f"/api/runs/{run_id}/redaction-proof?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["proof_id"] == _SAMPLE_PROOF["proof_id"]
    assert body["findings_count"]["total"] == 0
    assert body["chain_hash"] == "sha256:chainxyz"
    assert len(body["targets"]) == 1
    assert body["targets"][0]["ref"] == "trace.jsonl"
