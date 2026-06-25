"""Tests for C-1.6 — nova list --stale [--stale-days N].

Covers:
- list_stale_assets() returns assets inactive longer than stale_days
- list_stale_assets() excludes recently-active assets
- list_stale_assets() respects --stale-days threshold
- list_stale_assets() respects --status filter alongside --stale
- list_stale_assets() respects --type filter alongside --stale
- list_stale_assets() reports correct last_activity_at and days_stale
- CLI nova list --stale shows stale table
- CLI nova list --stale --stale-days N uses custom threshold
- CLI nova list --stale with no stale assets prints no-stale message
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.registry.service import (
    list_stale_assets,
    promote_asset,
    register_asset,
)
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_MODEL_YAML = """\
novafabric_spec_version: "1"
asset_type: model
name: stale-model
version: 1.0.0
status: development
description: model for stale tests
spec:
  framework: pytorch
  artifact_path: /tmp/model
"""

_TOOL_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: fresh-tool
version: 1.0.0
status: development
description: tool for stale tests
spec:
  entrypoint: /run
"""


def _write_spec(tmp_path: Path, content: str, filename: str) -> Path:
    p = tmp_path / filename
    p.write_text(content)
    return p


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_reg.db"


def _backdate_asset(db_path: Path, name: str, version: str, days_ago: int) -> None:
    """Manually backdate created_at and promoted_at to simulate old assets."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute(
        """
        UPDATE assets
        SET created_at = ?, promoted_at = ?
        WHERE name = ? AND version = ?
        """,
        (ts, ts, name, version),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestListStaleAssets:
    def test_stale_asset_is_returned(self, tmp_path: Path, db_path: Path) -> None:
        """An asset last active 31 days ago appears in stale list (threshold: 30)."""
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=31)

        stale = list_stale_assets(stale_days=30, db_path=db_path)
        names = [a["name"] for a in stale]
        assert "stale-model" in names

    def test_recent_asset_is_not_returned(self, tmp_path: Path, db_path: Path) -> None:
        """An asset last active 1 day ago is NOT stale (threshold: 30)."""
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)
        # No backdating — created_at is 'now'

        stale = list_stale_assets(stale_days=30, db_path=db_path)
        names = [a["name"] for a in stale]
        assert "stale-model" not in names

    def test_custom_threshold_respected(self, tmp_path: Path, db_path: Path) -> None:
        """stale_days=60 keeps an asset that is only 45 days old from appearing."""
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=45)

        stale_30 = list_stale_assets(stale_days=30, db_path=db_path)
        stale_60 = list_stale_assets(stale_days=60, db_path=db_path)

        names_30 = {a["name"] for a in stale_30}
        names_60 = {a["name"] for a in stale_60}

        assert "stale-model" in names_30
        assert "stale-model" not in names_60

    def test_status_filter_applied(self, tmp_path: Path, db_path: Path) -> None:
        """--status filter narrows stale results."""
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=40)

        # Filter for 'production' — asset is in 'development', should NOT appear
        stale_prod = list_stale_assets(
            stale_days=30, status="production", db_path=db_path
        )
        names = {a["name"] for a in stale_prod}
        assert "stale-model" not in names

        # Filter for 'development' — should appear
        stale_dev = list_stale_assets(
            stale_days=30, status="development", db_path=db_path
        )
        names_dev = {a["name"] for a in stale_dev}
        assert "stale-model" in names_dev

    def test_type_filter_applied(self, tmp_path: Path, db_path: Path) -> None:
        """--type filter narrows stale results."""
        model_path = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        tool_path = _write_spec(tmp_path, _TOOL_YAML, "tool.yaml")
        model_spec = validate_spec(model_path)
        tool_spec = validate_spec(tool_path)
        register_asset(model_spec, model_path, db_path=db_path)
        register_asset(tool_spec, tool_path, db_path=db_path)
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=40)
        _backdate_asset(db_path, "fresh-tool", "1.0.0", days_ago=40)

        stale_models = list_stale_assets(stale_days=30, asset_type="model", db_path=db_path)
        names = {a["name"] for a in stale_models}
        assert "stale-model" in names
        assert "fresh-tool" not in names

    def test_result_has_required_fields(self, tmp_path: Path, db_path: Path) -> None:
        """Stale asset rows include last_activity_at and days_stale."""
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=35)

        stale = list_stale_assets(stale_days=30, db_path=db_path)
        assert len(stale) == 1
        asset = stale[0]
        assert "last_activity_at" in asset
        assert "days_stale" in asset
        assert isinstance(asset["days_stale"], int)
        assert asset["days_stale"] >= 35

    def test_promotion_resets_staleness(self, tmp_path: Path, db_path: Path) -> None:
        """A recently promoted asset is NOT stale even if created_at is old."""
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)
        # Backdate created_at only; promoted_at will be set by promote_asset
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=40)
        # Now promote (sets promoted_at to now)
        promote_asset("stale-model", "1.0.0", AssetStatus.staging, "test-user", db_path=db_path)

        stale = list_stale_assets(stale_days=30, db_path=db_path)
        names = {a["name"] for a in stale}
        assert "stale-model" not in names

    def test_empty_registry_returns_empty(self, tmp_path: Path, db_path: Path) -> None:
        stale = list_stale_assets(stale_days=30, db_path=db_path)
        assert stale == []


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestListStaleCLI:
    def _register_stale(self, tmp_path: Path, db_path: Path) -> None:
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=40)

    def test_stale_flag_shows_stale_table(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        self._register_stale(tmp_path, db_path)
        result = runner.invoke(app, ["list", "--stale"])
        assert result.exit_code == 0, result.output
        assert "stale-model" in result.output
        assert "Days Stale" in result.output

    def test_stale_days_flag_custom_threshold(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        self._register_stale(tmp_path, db_path)
        # 40 days old — should appear with --stale-days 30
        result_30 = runner.invoke(app, ["list", "--stale", "--stale-days", "30"])
        assert result_30.exit_code == 0
        assert "stale-model" in result_30.output

        # Should NOT appear with --stale-days 60
        result_60 = runner.invoke(app, ["list", "--stale", "--stale-days", "60"])
        assert result_60.exit_code == 0
        assert "stale-model" not in result_60.output

    def test_no_stale_assets_prints_message(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        # Register a fresh asset — should not be stale
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)

        result = runner.invoke(app, ["list", "--stale"])
        assert result.exit_code == 0
        assert "No stale assets found" in result.output

    def test_stale_with_status_filter(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        self._register_stale(tmp_path, db_path)
        # Asset is in 'development', so filtering for 'production' should return nothing
        result = runner.invoke(
            app, ["list", "--stale", "--status", "production"]
        )
        assert result.exit_code == 0
        assert "stale-model" not in result.output

    def test_regular_list_unchanged(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)

        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "stale-model" in result.output
        # Should NOT have the stale columns when --stale is not passed
        assert "Days Stale" not in result.output


class TestListStaleWithEvalAndLineage:
    """Coverage for eval_results and lineage paths in list_stale_assets."""

    def test_eval_activity_resets_staleness(self, tmp_path: Path, db_path: Path) -> None:
        """A recent eval run on an old asset marks it as not stale."""
        from datetime import datetime, timezone

        from novafabric.registry.store import get_connection, init_schema

        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        result = register_asset(spec, p, db_path=db_path)
        asset_id = result["id"]
        # Backdate created_at to 40 days ago
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=40)

        # Insert a recent eval result (run_at = now)
        conn = get_connection(db_path)
        init_schema(conn)
        conn.execute(
            """
            INSERT INTO eval_results (id, asset_id, suite_name, passed, run_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "eval-001",
                asset_id,
                "smoke",
                1,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        stale = list_stale_assets(stale_days=30, db_path=db_path)
        names = {a["name"] for a in stale}
        assert "stale-model" not in names

    def test_consumed_lineage_resets_staleness(self, tmp_path: Path, db_path: Path) -> None:
        """An asset recently consumed (C-1.1 lineage) is not stale."""

        from novafabric.lineage._store import LineageStore
        from novafabric.lineage._types import LineageEdge

        p = _write_spec(tmp_path, _MODEL_YAML, "model.yaml")
        spec = validate_spec(p)
        register_asset(spec, p, db_path=db_path)
        _backdate_asset(db_path, "stale-model", "1.0.0", days_ago=40)

        # Write a consumed lineage edge dated today
        ls = LineageStore(db_path)
        edge = LineageEdge(
            edge_type="consumed",
            source={"kind": "run", "run_id": "RUN001"},
            target={"kind": "asset", "asset_ref": "stale-model@1.0.0"},
            confidence="observed",
            capsule_run_id="RUN001",
        )
        ls.insert_edge(edge)

        stale = list_stale_assets(stale_days=30, db_path=db_path)
        names = {a["name"] for a in stale}
        assert "stale-model" not in names

    def test_no_prev_production_only_in_db_fallback(
        self, tmp_path: Path, db_path: Path
    ) -> None:
        """rollback_asset uses DB fallback when audit log has no PROMOTE entries."""
        # Set up assets without an audit log (simulate fresh install)
        import novafabric.registry.service as _svc
        from novafabric.registry.service import rollback_asset

        # Redirect audit log to an empty file
        audit_path = tmp_path / "empty_audit.log"
        original = _svc.AUDIT_LOG_PATH
        _svc.AUDIT_LOG_PATH = audit_path
        try:
            v1_path = _write_spec(tmp_path, _MODEL_YAML, "v1.yaml")
            # Use a second model spec
            _MODEL2_YAML = _MODEL_YAML.replace("version: 1.0.0", "version: 2.0.0")
            v2_path = _write_spec(tmp_path, _MODEL2_YAML, "v2.yaml")
            spec1 = validate_spec(v1_path)
            spec2 = validate_spec(v2_path)
            register_asset(spec1, v1_path, db_path=db_path)
            register_asset(spec2, v2_path, db_path=db_path)
            # Manually set v2 to production in DB without going through service
            # (to avoid writing to audit log)
            from novafabric.registry.store import get_connection, init_schema

            conn = get_connection(db_path)
            init_schema(conn)
            conn.execute(
                "UPDATE assets SET status = 'production'"
                " WHERE name = 'stale-model' AND version = '2.0.0'"
            )
            conn.commit()
            conn.close()

            result = rollback_asset(
                "stale-model", target_version=None, actor="sre", db_path=db_path
            )
            assert result["archived_version"] == "2.0.0"
            assert result["restored_version"] == "1.0.0"
        finally:
            _svc.AUDIT_LOG_PATH = original
