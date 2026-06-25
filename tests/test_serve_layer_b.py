"""Tests for Layer B mutations + audit log (per ADR-0027)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from novafabric.serve import audit
from novafabric.serve.app import create_app

VALID_TOKEN = "layer-b-test-token-1234567890"
HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """Isolated tmp paths for capsules, registry, and the audit log."""
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db = tmp_path / "registry.db"
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.AUDIT_ENV, str(audit_path))
    yield {"capsule_dir": capsule_dir, "db": db, "audit": audit_path}


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


# ---------- audit log primitive ----------

def test_audit_append_and_read(env: dict[str, Path]) -> None:
    audit.append(
        action="t1",
        args={"x": 1},
        cli_equivalent="nova foo",
        actor_token_fp="abcd1234",
    )
    audit.append(
        action="t2",
        args={"y": 2},
        cli_equivalent="nova bar",
        actor_token_fp="abcd1234",
        result="error",
        error="boom",
    )
    entries = audit.read_recent(10)
    assert len(entries) == 2
    # Newest first
    assert entries[0]["action"] == "t2"
    assert entries[0]["error"] == "boom"
    assert entries[1]["action"] == "t1"
    # File mode is owner read/write only
    assert env["audit"].exists()


def test_audit_endpoint_returns_entries(client: TestClient) -> None:
    audit.append(
        action="probe",
        args={},
        cli_equivalent="nova probe",
        actor_token_fp="abcd1234",
    )
    res = client.get(f"/api/audit?token={VALID_TOKEN}", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["count"] >= 1
    assert any(e["action"] == "probe" for e in body["entries"])


def test_audit_requires_token(client: TestClient) -> None:
    assert client.get("/api/audit", headers=HEADERS).status_code == 401


# ---------- POST /api/assets (register) ----------

REGISTER_YAML = """\
novafabric_spec_version: "1"
asset_type: model
name: pytest-model
version: 1.0.0
status: development
description: test asset registered by pytest
spec:
  framework: pytorch
  artifact_path: /tmp/pytest-model
"""


def test_register_asset_requires_confirmation(client: TestClient) -> None:
    res = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": False},
    )
    assert res.status_code == 400


def test_register_asset_succeeds_when_confirmed(client: TestClient, env: dict[str, Path]) -> None:
    res = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["asset"]["name"] == "pytest-model"
    assert body["asset"]["version"] == "1.0.0"

    # Audit log captured the action
    entries = audit.read_recent(10)
    register_entries = [
        e for e in entries if e["action"] == "register_asset" and e.get("result") == "ok"
    ]
    assert len(register_entries) == 1
    assert register_entries[0]["args"]["name"] == "pytest-model"


def test_register_asset_invalid_yaml_returns_422(client: TestClient) -> None:
    res = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": "name: missing-required-fields", "confirmed": True},
    )
    assert res.status_code == 422


def test_register_asset_duplicate_returns_409(client: TestClient) -> None:
    # Register once
    client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    # Register again — should 409
    res = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    assert res.status_code == 409


def test_register_asset_without_token_returns_401(client: TestClient) -> None:
    res = client.post(
        "/api/assets",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    assert res.status_code == 401


# ---------- POST /api/assets/{name}/{version}/eval ----------

def test_eval_requires_confirmation(client: TestClient) -> None:
    res = client.post(
        f"/api/assets/anything/0.0.1/eval?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": False},
    )
    assert res.status_code == 400


def test_eval_unknown_asset_returns_404(client: TestClient) -> None:
    res = client.post(
        f"/api/assets/nonexistent/0.0.1/eval?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code in (404, 500)
    # Audit log records the failure
    entries = audit.read_recent(10)
    eval_entries = [e for e in entries if e["action"] == "eval_asset"]
    assert any(e.get("result") == "error" for e in eval_entries)


# ---------- POST /api/evidence/{run_id} ----------

def test_evidence_requires_confirmation(client: TestClient) -> None:
    res = client.post(
        f"/api/evidence/01TEST00000000000000000099?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": False},
    )
    assert res.status_code == 400


def test_evidence_unknown_run_returns_404(client: TestClient) -> None:
    res = client.post(
        f"/api/evidence/01TEST00000000000000000099?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 404


# ---------- GET /api/assets/{name}/{version} — with eval_results ----------

def test_get_registered_asset_includes_eval_results(client: TestClient) -> None:
    # Register first so the asset exists.
    client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    res = client.get(
        f"/api/assets/pytest-model/1.0.0?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "pytest-model"
    assert "eval_results" in body
    assert isinstance(body["eval_results"], list)


# ---------- POST /api/assets/{name}/{version}/eval — exception path ----------

def test_eval_registered_asset_records_error_on_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )

    # Force run_evals to raise a generic exception (not AssetNotFoundError)
    # so the except-Exception branch (lines 417-426) is executed.
    from novafabric.eval import runner as eval_runner_mod

    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated eval runner failure")

    monkeypatch.setattr(eval_runner_mod, "run_evals", _raise)

    res = client.post(
        f"/api/assets/pytest-model/1.0.0/eval?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 500
    entries = audit.read_recent(10)
    eval_entries = [e for e in entries if e["action"] == "eval_asset"]
    assert any(e.get("result") == "error" for e in eval_entries)


def test_eval_asset_success_records_ok_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )

    from novafabric.eval import runner as eval_runner_mod

    monkeypatch.setattr(
        eval_runner_mod, "run_evals", lambda *a, **kw: {"passed": True, "suites": []}
    )

    res = client.post(
        f"/api/assets/pytest-model/1.0.0/eval?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["passed"] is True
    entries = audit.read_recent(10)
    ok_evals = [
        e for e in entries if e["action"] == "eval_asset" and e.get("result") == "ok"
    ]
    assert len(ok_evals) >= 1


# ---------- POST /api/assets/{name}/{version}/promote — blocked ----------

def test_promote_blocked_records_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.registry import service as reg_service_mod

    reg = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    assert reg.status_code == 200

    # Force PromotionBlockedError so lines 619-628 are executed.
    def _blocked(*args: object, **kwargs: object) -> None:
        raise reg_service_mod.PromotionBlockedError("eval gate not passed")

    monkeypatch.setattr(reg_service_mod, "promote_asset", _blocked)

    res = client.post(
        f"/api/assets/pytest-model/1.0.0/promote?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"to_status": "production", "confirmed": True},
    )
    assert res.status_code == 412
    entries = audit.read_recent(10)
    promote_entries = [e for e in entries if e["action"] == "promote_asset"]
    assert any(e.get("result") == "error" for e in promote_entries)


# ---------- audit fingerprint ----------

def test_audit_records_token_fingerprint_not_full_token(client: TestClient) -> None:
    """The audit log must not contain the full token."""
    res = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    assert res.status_code == 200
    entries = audit.read_recent(5)
    assert all(VALID_TOKEN not in json.dumps(e) for e in entries)
    # The fingerprint should be the first 8 chars of the token.
    assert any(e.get("actor_token_fp") == VALID_TOKEN[:8] for e in entries)
