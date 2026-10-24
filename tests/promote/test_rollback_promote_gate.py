"""Rollback and every single-actor promote route share the ADR-0312 D1 gate.

Attack (adversarial review, 2026-10-22): ``rollback_asset`` moved its target
straight to ``production`` with no ``promote_direct`` gate. Over HTTP an
``operate`` token promoted an unreviewed v2 to ``staging`` (ungated), then
``POST /api/assets/{name}/rollback`` picked v2 as "the previous version" and put
it in production. ``nova rollback --to <draft>`` did the same for a version that
had never been anywhere near production.

The fix: a rollback restores, ungated, only a version the registry's status
history shows was in production before. Any other target is a single-actor move
into a protected stage and goes through exactly the gate ``nova promote direct``
uses. The ``nova server`` route ``PUT /v0/assets/{id}/promote`` and
``nova prompt promote`` now call that same gate too.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app as cli_app
from novafabric.policy import NoopEngine
from novafabric.policy._models import PolicyDecision, PolicyInput
from novafabric.registry import service as svc
from novafabric.registry import store as registry_store
from novafabric.registry.service import (
    DirectPromotionDeniedError,
    InvalidLifecycleTransitionError,
    RollbackError,
    get_asset,
    promote_asset,
    register_asset,
    rollback_asset,
)
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

runner = CliRunner()
NAME = "fraud-model"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "valid_model.yaml"


class _AllowAll:
    """An OPA stand-in that explicitly allows every action."""

    def __init__(self) -> None:
        self.inputs: list[PolicyInput] = []

    def evaluate(self, input_: PolicyInput) -> PolicyDecision:
        self.inputs.append(input_)
        return PolicyDecision(allow=True, reason="test allow", decision_id="t-allow")


@dataclass
class Reg:
    db: Path
    tmp: Path

    def status(self, version: str) -> str:
        return str(get_asset(NAME, version, db_path=self.db)["status"])

    def register(self, version: str) -> None:
        spec_path = self.tmp / f"v{version}.yaml"
        spec_path.write_text(FIXTURE.read_text().replace("1.0.0", version))
        register_asset(validate_spec(spec_path), spec_path, db_path=self.db)


@pytest.fixture
def reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Reg]:
    """v1 legitimately in production; v2 and v3 are unreviewed drafts."""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "registry.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    r = Reg(db=db, tmp=tmp_path)
    r.register("1.0.0")
    promote_asset(NAME, "1.0.0", AssetStatus.production, actor="owner", db_path=db)
    r.register("2.0.0")
    r.register("3.0.0")
    yield r


def _noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.registry.service.get_policy_engine", NoopEngine)


def _allow(monkeypatch: pytest.MonkeyPatch) -> _AllowAll:
    engine = _AllowAll()
    monkeypatch.setattr("novafabric.registry.service.get_policy_engine", lambda: engine)
    return engine


# ---------------------------------------------------------------------------
# Service layer (shared by CLI, nova serve)
# ---------------------------------------------------------------------------


class TestRollbackService:
    def test_auto_rollback_never_picks_a_version_that_was_not_in_production(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The attack: v2 dev -> staging (ungated), then auto-rollback."""
        _noop(monkeypatch)
        promote_asset(NAME, "2.0.0", AssetStatus.staging, actor="mallory", db_path=reg.db)
        with pytest.raises(RollbackError, match="No previous production version"):
            rollback_asset(NAME, None, actor="mallory", db_path=reg.db)
        assert reg.status("1.0.0") == "production"
        assert reg.status("2.0.0") == "staging"

    def test_explicit_rollback_to_a_never_promoted_draft_is_gated(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _noop(monkeypatch)
        with pytest.raises(DirectPromotionDeniedError, match="promote_direct"):
            rollback_asset(NAME, "3.0.0", actor="mallory", db_path=reg.db)
        assert reg.status("1.0.0") == "production"
        assert reg.status("3.0.0") == "development"

    def test_gated_rollback_uses_the_authenticated_policy_subject(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = _allow(monkeypatch)
        result = rollback_asset(
            NAME, "3.0.0", actor="label", db_path=reg.db, policy_subject="serve:abc"
        )
        assert result["restored_version"] == "3.0.0"
        assert result["gate"] == "promote_direct"
        direct = [i for i in engine.inputs if i.action == "promote_direct"]
        assert direct and direct[0].subject.user == "serve:abc"
        assert reg.status("3.0.0") == "production"
        assert reg.status("1.0.0") == "archived"

    def test_gated_rollback_keeps_the_lifecycle_rules(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An archived version that never reached production stays archived."""
        _allow(monkeypatch)
        promote_asset(NAME, "3.0.0", AssetStatus.archived, actor="x", db_path=reg.db)
        with pytest.raises(InvalidLifecycleTransitionError):
            rollback_asset(NAME, "3.0.0", actor="x", db_path=reg.db)
        assert reg.status("1.0.0") == "production"

    def test_restoring_a_previous_production_version_needs_no_policy(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The legitimate rollback (incident response) keeps working without OPA."""
        _allow(monkeypatch)  # v2 reaches production through an explicit allow
        promote_asset(NAME, "1.0.0", AssetStatus.archived, actor="owner", db_path=reg.db)
        promote_asset(NAME, "2.0.0", AssetStatus.production, actor="owner", db_path=reg.db)
        _noop(monkeypatch)
        result = rollback_asset(NAME, None, actor="sre", db_path=reg.db)
        assert result["restored_version"] == "1.0.0"
        assert result["gate"] == "previously_production"
        assert reg.status("1.0.0") == "production"
        assert reg.status("2.0.0") == "archived"
        # ... and back again: v2 has production history too.
        again = rollback_asset(NAME, "2.0.0", actor="sre", db_path=reg.db)
        assert again["gate"] == "previously_production"
        assert reg.status("2.0.0") == "production"

    def test_history_records_every_production_entry(self, reg: Reg) -> None:
        assert svc.production_history_versions(NAME, db_path=reg.db) == ["1.0.0"]

    def test_a_legacy_registry_backfills_current_production(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registry created before the history table keeps its production versions."""
        conn = sqlite3.connect(reg.db)
        conn.execute("DROP TABLE asset_status_history")
        conn.commit()
        conn.close()
        registry_store._SCHEMA_READY.clear()
        assert svc.production_history_versions(NAME, db_path=reg.db) == ["1.0.0"]


# ---------------------------------------------------------------------------
# CLI: nova rollback
# ---------------------------------------------------------------------------


class TestRollbackCLI:
    def test_rollback_to_a_draft_is_refused(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _noop(monkeypatch)
        res = runner.invoke(cli_app, ["rollback", NAME, "--to", "3.0.0", "--db", str(reg.db)])
        assert isinstance(res.exception, SystemExit), res.exception
        assert res.exit_code == 1, res.output
        assert "promote_direct" in res.output
        assert reg.status("3.0.0") == "development"
        assert reg.status("1.0.0") == "production"


# ---------------------------------------------------------------------------
# HTTP: nova serve  POST /api/assets/{name}/rollback
# ---------------------------------------------------------------------------

TOKEN = "rollback-gate-token-0123456789abcdef"
H = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


def _serve_client(reg: Reg) -> Any:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    caps = reg.tmp / "caps"
    caps.mkdir(exist_ok=True)
    return TestClient(create_app(token=TOKEN, capsule_dir=caps, db_path=reg.db, static_dir=None))


class TestServeRollback:
    def test_staging_then_rollback_does_not_reach_production(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "dash.jsonl"))
        _noop(monkeypatch)
        c = _serve_client(reg)
        r = c.post(
            f"/api/assets/{NAME}/2.0.0/promote",
            headers=H,
            json={"to_status": "staging", "confirmed": True},
        )
        assert r.status_code == 200, r.text
        r = c.post(f"/api/assets/{NAME}/rollback", headers=H, json={"confirmed": True})
        assert r.status_code == 409, r.text
        assert reg.status("2.0.0") == "staging"
        assert reg.status("1.0.0") == "production"

    def test_legitimate_rollback_still_works(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "dash.jsonl"))
        _allow(monkeypatch)
        promote_asset(NAME, "1.0.0", AssetStatus.archived, actor="owner", db_path=reg.db)
        promote_asset(NAME, "2.0.0", AssetStatus.production, actor="owner", db_path=reg.db)
        _noop(monkeypatch)
        c = _serve_client(reg)
        r = c.post(f"/api/assets/{NAME}/rollback", headers=H, json={"confirmed": True})
        assert r.status_code == 200, r.text
        assert r.json()["result"]["restored_version"] == "1.0.0"


# ---------------------------------------------------------------------------
# HTTP: nova server  PUT /v0/assets/{id}/promote
# ---------------------------------------------------------------------------


def _server_client(reg: Reg) -> Any:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.server.app import create_app
    from novafabric.server.config import ServerConfig

    app = create_app(ServerConfig(db_path=str(reg.db), insecure_no_auth=True))
    return TestClient(app, raise_server_exceptions=False)


class TestServerPromote:
    def _id(self, reg: Reg, version: str) -> str:
        return str(get_asset(NAME, version, db_path=reg.db)["id"])

    def test_production_is_refused_without_an_explicit_allow(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _noop(monkeypatch)
        c = _server_client(reg)
        r = c.put(
            f"/v0/assets/{self._id(reg, '2.0.0')}/promote",
            json={"to_status": "production", "actor": "x", "force": True},
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "promote_direct_denied"
        assert reg.status("2.0.0") == "development"

    def test_staging_is_unaffected(self, reg: Reg, monkeypatch: pytest.MonkeyPatch) -> None:
        _noop(monkeypatch)
        c = _server_client(reg)
        r = c.put(
            f"/v0/assets/{self._id(reg, '2.0.0')}/promote",
            json={"to_status": "staging", "actor": "x"},
        )
        assert r.status_code == 200, r.text
        assert reg.status("2.0.0") == "staging"

    def test_promoted_by_names_the_authenticated_principal(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # review server-18: the caller's free-text `actor` became the
        # provenance record, so any writer could promote "as" someone else.
        _noop(monkeypatch)
        c = _server_client(reg)
        r = c.put(
            f"/v0/assets/{self._id(reg, '2.0.0')}/promote",
            json={"to_status": "staging", "actor": "cfo@corp"},
        )
        assert r.status_code == 200, r.text
        assert get_asset(NAME, "2.0.0", db_path=reg.db)["promoted_by"] == "server:local"

    def test_an_explicit_allow_permits_production(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = _allow(monkeypatch)
        c = _server_client(reg)
        r = c.put(
            f"/v0/assets/{self._id(reg, '2.0.0')}/promote",
            json={"to_status": "production", "actor": "x"},
        )
        assert r.status_code == 200, r.text
        direct = [i for i in engine.inputs if i.action == "promote_direct"]
        assert direct and direct[0].subject.user.startswith("server:")


# ---------------------------------------------------------------------------
# CLI: nova prompt promote
# ---------------------------------------------------------------------------


class TestPromptPromote:
    def test_prompt_promote_to_production_is_gated(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _noop(monkeypatch)
        monkeypatch.setattr(
            "novafabric.registry.service.get_asset",
            lambda name, version: {"asset_type": "prompt", "name": name},
        )
        promoted: list[object] = []
        monkeypatch.setattr(
            "novafabric.registry.service.promote_asset",
            lambda *a, **k: promoted.append(a) or {"status": "production"},
        )
        res = runner.invoke(
            cli_app, ["prompt", "promote", f"{NAME}@2.0.0", "--to", "production"]
        )
        assert isinstance(res.exception, SystemExit), res.exception
        assert res.exit_code == 1, res.output
        assert "promote_direct" in res.output
        assert promoted == []
