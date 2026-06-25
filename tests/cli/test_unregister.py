"""Tests for nova unregister command (AI-7).

Covers:
- Service: removes asset row, eval_results, approvals
- Service: blocks staging/production/pending_approval without --force
- Service: --force allows deletion of active-lifecycle assets
- Service: raises AssetNotFoundError for missing assets
- Service: writes UNREGISTER audit entry
- CLI: success with --yes flag
- CLI: asset not found → exit 1
- CLI: blocked production → exit 1
- CLI: --force --yes on production → exit 0
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.audit import AuditEventType
from novafabric.audit._models import AuditEntry
from novafabric.cli.main import app
from novafabric.registry.service import (
    AssetNotFoundError,
    UnregisterBlockedError,
    get_asset,
    promote_asset,
    register_asset,
    unregister_asset,
)
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

runner = CliRunner()

# ---------------------------------------------------------------------------
# Inline spec fixtures
# ---------------------------------------------------------------------------

_TOOL_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: unreg-tool
version: 1.0.0
status: development
description: tool for unregister tests
spec:
  entrypoint: /run
"""


def _write_spec(tmp_path: Path, content: str, filename: str = "spec.yaml") -> Path:
    p = tmp_path / filename
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_unreg.db"


@pytest.fixture
def audit_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_path = tmp_path / "audit.log"
    import novafabric.audit._paths as _paths

    monkeypatch.setattr(_paths, "AUDIT_LOG_PATH", log_path)
    import novafabric.registry.service as _svc

    monkeypatch.setattr(_svc, "AUDIT_LOG_PATH", log_path)
    return log_path


@pytest.fixture
def registered_tool(tmp_path: Path, db_path: Path) -> dict:
    spec_path = _write_spec(tmp_path, _TOOL_YAML)
    spec = validate_spec(spec_path)
    return register_asset(spec, spec_path, db_path=db_path)


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestUnregisterAssetService:
    def test_removes_development_asset(
        self, registered_tool: dict, db_path: Path, audit_log_path: Path
    ) -> None:
        unregister_asset("unreg-tool", "1.0.0", actor="tester", db_path=db_path)
        with pytest.raises(AssetNotFoundError):
            get_asset("unreg-tool", "1.0.0", db_path=db_path)

    def test_removes_eval_results(
        self, registered_tool: dict, db_path: Path, audit_log_path: Path
    ) -> None:
        asset_id = registered_tool["id"]
        conn = get_connection(db_path)
        init_schema(conn)
        import uuid
        from datetime import datetime, timezone

        conn.execute(
            "INSERT INTO eval_results (id, asset_id, suite_name, passed, score_json, run_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), asset_id, "smoke", 1, "{}", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        unregister_asset("unreg-tool", "1.0.0", actor="tester", db_path=db_path)

        conn2 = get_connection(db_path)
        row = conn2.execute(
            "SELECT id FROM eval_results WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        conn2.close()
        assert row is None

    def test_removes_approvals(
        self, registered_tool: dict, db_path: Path, audit_log_path: Path
    ) -> None:
        conn = get_connection(db_path)
        init_schema(conn)
        import uuid
        from datetime import datetime, timezone

        conn.execute(
            "INSERT INTO approvals (approval_id, asset_name, asset_version, approver, approved_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                "unreg-tool",
                "1.0.0",
                "approver-x",
                datetime.now(timezone.utc).isoformat(),
                "",
            ),
        )
        conn.commit()
        conn.close()

        unregister_asset("unreg-tool", "1.0.0", actor="tester", db_path=db_path)

        conn2 = get_connection(db_path)
        row = conn2.execute(
            "SELECT approval_id FROM approvals WHERE asset_name = ? AND asset_version = ?",
            ("unreg-tool", "1.0.0"),
        ).fetchone()
        conn2.close()
        assert row is None

    def test_blocks_production_without_force(
        self, tmp_path: Path, registered_tool: dict, db_path: Path, audit_log_path: Path
    ) -> None:
        promote_asset("unreg-tool", "1.0.0", AssetStatus.production, "tester", db_path=db_path)
        with pytest.raises(UnregisterBlockedError, match="production"):
            unregister_asset("unreg-tool", "1.0.0", actor="tester", db_path=db_path)

    def test_blocks_staging_without_force(
        self, registered_tool: dict, db_path: Path, audit_log_path: Path
    ) -> None:
        promote_asset("unreg-tool", "1.0.0", AssetStatus.staging, "tester", db_path=db_path)
        with pytest.raises(UnregisterBlockedError, match="staging"):
            unregister_asset("unreg-tool", "1.0.0", actor="tester", db_path=db_path)

    def test_force_allows_production(
        self, registered_tool: dict, db_path: Path, audit_log_path: Path
    ) -> None:
        promote_asset("unreg-tool", "1.0.0", AssetStatus.production, "tester", db_path=db_path)
        result = unregister_asset(
            "unreg-tool", "1.0.0", actor="tester", force=True, db_path=db_path
        )
        assert result["name"] == "unreg-tool"
        with pytest.raises(AssetNotFoundError):
            get_asset("unreg-tool", "1.0.0", db_path=db_path)

    def test_missing_asset_raises(self, db_path: Path, audit_log_path: Path) -> None:
        with pytest.raises(AssetNotFoundError, match="not found"):
            unregister_asset("ghost", "9.9.9", actor="tester", db_path=db_path)

    def test_writes_audit_entry(
        self, registered_tool: dict, db_path: Path, audit_log_path: Path
    ) -> None:
        unregister_asset("unreg-tool", "1.0.0", actor="audit-tester", db_path=db_path)

        entries = [
            AuditEntry.model_validate_json(line)
            for line in audit_log_path.read_text().splitlines()
            if line.strip()
        ]
        unreg_entries = [e for e in entries if e.event_type == AuditEventType.UNREGISTER]
        assert len(unreg_entries) == 1
        entry = unreg_entries[0]
        assert entry.actor == "audit-tester"
        assert entry.resource_id == "unreg-tool@1.0.0"
        assert entry.details["status_at_deletion"] == "development"
        assert entry.details["forced"] is False

    def test_archived_asset_is_allowed(
        self, registered_tool: dict, db_path: Path, audit_log_path: Path
    ) -> None:
        promote_asset("unreg-tool", "1.0.0", AssetStatus.archived, "tester", db_path=db_path)
        result = unregister_asset("unreg-tool", "1.0.0", actor="tester", db_path=db_path)
        assert result["name"] == "unreg-tool"
        assert result["status"] == "archived"


# ---------------------------------------------------------------------------
# CLI-level tests
# ---------------------------------------------------------------------------


class TestUnregisterCLI:
    def _register(self, tmp_path: Path, db_path: Path) -> None:
        spec_path = _write_spec(tmp_path, _TOOL_YAML)
        spec = validate_spec(spec_path)
        register_asset(spec, spec_path, db_path=db_path)

    def test_cli_success_with_yes_flag(
        self,
        tmp_path: Path,
        db_path: Path,
        audit_log_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._register(tmp_path, db_path)
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(app, ["unregister", "unreg-tool@1.0.0", "--yes"])
        assert result.exit_code == 0, result.output
        assert "unreg-tool" in result.output

    def test_cli_asset_not_found(
        self,
        db_path: Path,
        audit_log_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(app, ["unregister", "ghost@9.9.9", "--yes"])
        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_cli_blocked_production(
        self,
        tmp_path: Path,
        db_path: Path,
        audit_log_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._register(tmp_path, db_path)
        promote_asset("unreg-tool", "1.0.0", AssetStatus.production, "tester", db_path=db_path)
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(app, ["unregister", "unreg-tool@1.0.0", "--yes"])
        assert result.exit_code == 1
        assert "Blocked" in result.output

    def test_cli_force_overrides_block(
        self,
        tmp_path: Path,
        db_path: Path,
        audit_log_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._register(tmp_path, db_path)
        promote_asset("unreg-tool", "1.0.0", AssetStatus.production, "tester", db_path=db_path)
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(app, ["unregister", "unreg-tool@1.0.0", "--force", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Unregistered" in result.output

    def test_cli_missing_version_in_ref(
        self,
        monkeypatch: pytest.MonkeyPatch,
        db_path: Path,
        audit_log_path: Path,
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(app, ["unregister", "unreg-tool", "--yes"])
        assert result.exit_code == 1
        assert "name@version" in result.output
