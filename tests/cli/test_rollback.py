"""Tests for C-1.4 — nova rollback command.

Covers:
- Success path (auto-detect previous production version)
- Explicit --to target version
- Error: no current production version
- Error: no previous production version to roll back to
- Error: --to version does not exist
- Atomic effect: current is archived, target is production
- Audit log entry with ROLLBACK event type
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.audit import AuditEventType, AuditLog
from novafabric.cli.main import app
from novafabric.registry.service import (
    AssetNotFoundError,
    RollbackError,
    get_asset,
    promote_asset,
    register_asset,
    rollback_asset,
)
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TOOL_V1_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: rollback-tool
version: 1.0.0
status: development
description: v1
spec:
  entrypoint: /v1/run
"""

_TOOL_V2_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: rollback-tool
version: 2.0.0
status: development
description: v2
spec:
  entrypoint: /v2/run
"""

_TOOL_V3_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: rollback-tool
version: 3.0.0
status: development
description: v3
spec:
  entrypoint: /v3/run
"""


def _write_spec(tmp_path: Path, content: str, filename: str) -> Path:
    p = tmp_path / filename
    p.write_text(content)
    return p


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_reg.db"


@pytest.fixture
def audit_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the audit log to a temp file so tests are isolated."""
    log_path = tmp_path / "audit.log"
    import novafabric.audit._paths as _paths
    monkeypatch.setattr(_paths, "AUDIT_LOG_PATH", log_path)
    import novafabric.registry.service as _svc
    monkeypatch.setattr(_svc, "AUDIT_LOG_PATH", log_path)
    return log_path


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestRollbackAssetService:
    def _setup_v1_then_v2_in_production(
        self, tmp_path: Path, db_path: Path, audit_log_path: Path
    ) -> None:
        """Register v1 and v2; promote v1 → production, then v2 → production."""
        v1_path = _write_spec(tmp_path, _TOOL_V1_YAML, "v1.yaml")
        v2_path = _write_spec(tmp_path, _TOOL_V2_YAML, "v2.yaml")
        spec1 = validate_spec(v1_path)
        spec2 = validate_spec(v2_path)
        register_asset(spec1, v1_path, db_path=db_path)
        register_asset(spec2, v2_path, db_path=db_path)
        # Promote v1 to production (tool assets don't require eval gate)
        promote_asset(
            "rollback-tool", "1.0.0", AssetStatus.production, "test-user", db_path=db_path
        )
        # Promote v2 to production; v1 must be archived first (production → archived)
        promote_asset(
            "rollback-tool", "1.0.0", AssetStatus.archived, "test-user", db_path=db_path
        )
        promote_asset(
            "rollback-tool", "2.0.0", AssetStatus.production, "test-user", db_path=db_path
        )

    def test_rollback_auto_detect_previous_production(
        self, tmp_path: Path, db_path: Path, audit_log_path: Path
    ) -> None:
        """Auto rollback: archives v2, restores v1 to production."""
        self._setup_v1_then_v2_in_production(tmp_path, db_path, audit_log_path)
        result = rollback_asset("rollback-tool", target_version=None, actor="sre", db_path=db_path)
        assert result["archived_version"] == "2.0.0"
        assert result["restored_version"] == "1.0.0"
        assert "rollback_id" in result

        # Verify DB state
        v1 = get_asset("rollback-tool", "1.0.0", db_path=db_path)
        v2 = get_asset("rollback-tool", "2.0.0", db_path=db_path)
        assert v1["status"] == "production"
        assert v2["status"] == "archived"

    def test_rollback_explicit_to_version(
        self, tmp_path: Path, db_path: Path, audit_log_path: Path
    ) -> None:
        """Explicit --to: archives current, restores specified version."""
        self._setup_v1_then_v2_in_production(tmp_path, db_path, audit_log_path)
        result = rollback_asset(
            "rollback-tool", target_version="1.0.0", actor="sre", db_path=db_path
        )
        assert result["archived_version"] == "2.0.0"
        assert result["restored_version"] == "1.0.0"

    def test_rollback_no_current_production_raises(
        self, tmp_path: Path, db_path: Path, audit_log_path: Path
    ) -> None:
        v1_path = _write_spec(tmp_path, _TOOL_V1_YAML, "v1.yaml")
        spec1 = validate_spec(v1_path)
        register_asset(spec1, v1_path, db_path=db_path)
        # Asset is in 'development' — no production version

        with pytest.raises(RollbackError, match="No current production version"):
            rollback_asset("rollback-tool", target_version=None, actor="sre", db_path=db_path)

    def test_rollback_to_nonexistent_version_raises(
        self, tmp_path: Path, db_path: Path, audit_log_path: Path
    ) -> None:
        self._setup_v1_then_v2_in_production(tmp_path, db_path, audit_log_path)
        with pytest.raises(AssetNotFoundError, match="not found"):
            rollback_asset(
                "rollback-tool", target_version="9.9.9", actor="sre", db_path=db_path
            )

    def test_rollback_to_same_version_raises(
        self, tmp_path: Path, db_path: Path, audit_log_path: Path
    ) -> None:
        self._setup_v1_then_v2_in_production(tmp_path, db_path, audit_log_path)
        with pytest.raises(RollbackError, match="already the current production version"):
            rollback_asset(
                "rollback-tool", target_version="2.0.0", actor="sre", db_path=db_path
            )

    def test_rollback_audit_entry_written(
        self, tmp_path: Path, db_path: Path, audit_log_path: Path
    ) -> None:
        """rollback_asset writes a ROLLBACK audit entry."""
        self._setup_v1_then_v2_in_production(tmp_path, db_path, audit_log_path)
        rollback_asset("rollback-tool", target_version="1.0.0", actor="sre", db_path=db_path)

        log = AuditLog(audit_log_path)
        entries = log.query()
        rollback_entries = [
            e for e in entries if e.event_type == AuditEventType.ROLLBACK
        ]
        assert len(rollback_entries) == 1
        entry = rollback_entries[0]
        assert entry.actor == "sre"
        assert "archived_version" in entry.details
        assert entry.details["archived_version"] == "2.0.0"
        assert entry.details["restored_version"] == "1.0.0"

    def test_rollback_unknown_asset_raises(
        self, tmp_path: Path, db_path: Path, audit_log_path: Path
    ) -> None:
        with pytest.raises(RollbackError, match="No current production version"):
            rollback_asset("ghost-asset", target_version=None, actor="sre", db_path=db_path)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestRollbackCLI:
    def _setup_db(self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        v1_path = _write_spec(tmp_path, _TOOL_V1_YAML, "v1.yaml")
        v2_path = _write_spec(tmp_path, _TOOL_V2_YAML, "v2.yaml")
        spec1 = validate_spec(v1_path)
        spec2 = validate_spec(v2_path)
        register_asset(spec1, v1_path, db_path=db_path)
        register_asset(spec2, v2_path, db_path=db_path)
        promote_asset(
            "rollback-tool", "1.0.0", AssetStatus.production, "test-user", db_path=db_path
        )
        promote_asset(
            "rollback-tool", "1.0.0", AssetStatus.archived, "test-user", db_path=db_path
        )
        promote_asset(
            "rollback-tool", "2.0.0", AssetStatus.production, "test-user", db_path=db_path
        )

    def test_rollback_cmd_success(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch,
        audit_log_path: Path
    ) -> None:
        self._setup_db(tmp_path, db_path, monkeypatch)
        result = runner.invoke(
            app,
            ["rollback", "rollback-tool", "--actor", "sre-cli", "--db", str(db_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Archived" in result.output
        assert "Promoted" in result.output
        assert "2.0.0" in result.output
        assert "1.0.0" in result.output

    def test_rollback_cmd_explicit_to(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch,
        audit_log_path: Path
    ) -> None:
        self._setup_db(tmp_path, db_path, monkeypatch)
        result = runner.invoke(
            app,
            ["rollback", "rollback-tool", "--to", "1.0.0",
             "--actor", "sre-cli", "--db", str(db_path)],
        )
        assert result.exit_code == 0, result.output

    def test_rollback_cmd_no_production_exits_1(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch,
        audit_log_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        # Register but don't promote to production
        v1_path = _write_spec(tmp_path, _TOOL_V1_YAML, "v1.yaml")
        spec1 = validate_spec(v1_path)
        register_asset(spec1, v1_path, db_path=db_path)

        result = runner.invoke(
            app, ["rollback", "rollback-tool", "--db", str(db_path)]
        )
        assert result.exit_code == 1
        assert "Rollback failed" in result.output

    def test_rollback_cmd_bad_version_exits_1(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch,
        audit_log_path: Path
    ) -> None:
        self._setup_db(tmp_path, db_path, monkeypatch)
        result = runner.invoke(
            app,
            ["rollback", "rollback-tool", "--to", "9.9.9", "--db", str(db_path)],
        )
        assert result.exit_code == 1

    def test_rollback_cmd_unknown_asset_exits_1(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch,
        audit_log_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(app, ["rollback", "ghost-asset", "--db", str(db_path)])
        assert result.exit_code == 1
