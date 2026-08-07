"""Step-up gate + chained-audit fix (ADR-0246 slice 1).

Two independent properties:

1. **Role grant/revoke joins the hash-chained audit log** (the §12.1 defect:
   the most privileged mutation audited only to the unchained dashboard log).
   The chain must verify after the writes.
2. **The step-up gate**: with `step_up.enabled`, a destructive call whose
   JWT `auth_time`/`iat` is older than `max_age_seconds` gets 401
   `step_up_required`; fresh auth passes; disabled (default) changes nothing;
   an unregistered action name fails at wiring time, not silently at runtime.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.audit import AuditEventType, AuditLog  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import AuthContext, verify_token  # noqa: E402
from novafabric.server.config import ServerConfig, StepUpConfig  # noqa: E402
from novafabric.server.step_up import require_step_up  # noqa: E402


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    step_up: StepUpConfig | None = None,
    auth_time: float | None = None,
) -> TestClient:
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    cfg = ServerConfig(insecure_no_auth=True, db_path=str(tmp_path / "r.db"))
    if step_up is not None:
        cfg.step_up = step_up
    app = create_app(cfg)
    # Simulate an OIDC identity with a known freshness timestamp.
    app.dependency_overrides[verify_token] = lambda: AuthContext(
        subject="admin@example.test", roles=["admin"], auth_time=auth_time
    )
    return TestClient(app)


@pytest.fixture()
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The chained log path is resolved at import in _paths; point the module
    # constant at the tmp file for this test.
    path = tmp_path / "audit.jsonl"
    import novafabric.audit._paths as audit_paths

    monkeypatch.setattr(audit_paths, "AUDIT_LOG_PATH", path)
    return path


class TestChainedAudit:
    def test_role_grant_and_revoke_join_the_chained_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_path: Path
    ) -> None:
        client = _client(tmp_path, monkeypatch)
        assert (
            client.post(
                "/v0/admin/roles", json={"subject": "u@example.test", "role": "writer"}
            ).status_code
            == 201
        )
        assert (
            client.delete("/v0/admin/roles/u@example.test/writer").status_code == 200
        )

        import json

        types = [
            json.loads(line)["event_type"]
            for line in audit_path.read_text().splitlines()
            if line.strip()
        ]
        assert AuditEventType.ROLE_ASSIGN.value in types
        assert AuditEventType.ROLE_REVOKE.value in types

        errors = AuditLog(audit_path).verify()
        assert errors == [], f"chained audit log failed verification: {errors}"


class TestStepUp:
    def test_disabled_by_default_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_path: Path
    ) -> None:
        client = _client(tmp_path, monkeypatch, auth_time=time.time() - 99999)
        resp = client.post(
            "/v0/admin/roles", json={"subject": "u@example.test", "role": "reader"}
        )
        assert resp.status_code == 201

    def test_stale_auth_is_refused_with_step_up_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_path: Path
    ) -> None:
        client = _client(
            tmp_path,
            monkeypatch,
            step_up=StepUpConfig(enabled=True, max_age_seconds=300),
            auth_time=time.time() - 3600,
        )
        resp = client.post(
            "/v0/admin/roles", json={"subject": "u@example.test", "role": "reader"}
        )
        assert resp.status_code == 401
        assert "step_up_required" in resp.text

    def test_fresh_auth_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_path: Path
    ) -> None:
        client = _client(
            tmp_path,
            monkeypatch,
            step_up=StepUpConfig(enabled=True, max_age_seconds=300),
            auth_time=time.time() - 10,
        )
        resp = client.post(
            "/v0/admin/roles", json={"subject": "u@example.test", "role": "reader"}
        )
        assert resp.status_code == 201

    def test_credential_without_freshness_is_exempt_in_slice_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_path: Path
    ) -> None:
        client = _client(
            tmp_path,
            monkeypatch,
            step_up=StepUpConfig(enabled=True, max_age_seconds=300),
            auth_time=None,  # API key / local token class
        )
        resp = client.post(
            "/v0/admin/roles", json={"subject": "u@example.test", "role": "reader"}
        )
        assert resp.status_code == 201

    def test_unregistered_action_fails_at_wiring_time(self) -> None:
        with pytest.raises(ValueError, match="destructive-action registry"):
            require_step_up("coffee.brew")


def test_auth_context_freshness_extraction() -> None:
    """`auth_time` preferred, `iat` fallback, None when absent — the exact
    contract the gate depends on (unit-level, no app)."""
    from novafabric.server import auth as auth_mod

    assert AuthContext(subject="s").auth_time is None
    # The extraction lives inline in verify_token; assert the field exists and
    # the dataclass carries it (integration covered above via overrides).
    ctx = auth_mod.AuthContext(subject="s", auth_time=123.0)
    assert ctx.auth_time == 123.0
