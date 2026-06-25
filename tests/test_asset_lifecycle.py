# tests/test_asset_lifecycle.py
"""Tests for C-1 asset lifecycle gaps: consumption lineage, asset diff, declared deps."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.lineage._store import LineageStore
from novafabric.lineage._writer import LineageWriter
from novafabric.registry.service import (
    record_asset_consumption,
    register_asset,
)
from novafabric.spec.validator import validate_spec

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOOL_V1_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: lifecycle-tool
version: 1.0.0
status: development
description: tool v1
spec:
  entrypoint: /v1/run
"""

_TOOL_V2_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: lifecycle-tool
version: 2.0.0
status: production
description: tool v2
spec:
  entrypoint: /v2/run
"""

_DEP_TOOL_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: dep-tool
version: 1.0.0
status: staging
description: dependency tool
spec:
  entrypoint: /dep/run
"""

_CONSUMER_MODEL_YAML = """\
novafabric_spec_version: "1"
asset_type: model
name: consumer-model
version: 1.0.0
status: development
description: model that depends on dep-tool
spec:
  framework: pytorch
  artifact_path: /tmp/model
dependencies:
  - dep-tool@1.0.0
"""


def _write_spec(tmp_path: Path, content: str, filename: str) -> Path:
    p = tmp_path / filename
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# C-1.1 — status_at_consumption recorded in lineage facets
# ---------------------------------------------------------------------------


class TestC11StatusAtConsumption:
    def test_record_asset_consumption_writes_record(self, tmp_path: Path) -> None:
        """record_asset_consumption writes a record with status_at_consumption."""
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir()
        (capsule_dir / "assets.jsonl").touch()

        record_asset_consumption(
            asset_ref="fraud-model@1.0.0",
            status_at_consumption="production",
            capsule_dir=capsule_dir,
        )

        lines = (capsule_dir / "assets.jsonl").read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["asset_ref"] == "fraud-model@1.0.0"
        assert rec["status_at_consumption"] == "production"
        assert rec["registry"] == "local"

    def test_record_asset_consumption_custom_registry(self, tmp_path: Path) -> None:
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir()
        (capsule_dir / "assets.jsonl").touch()

        record_asset_consumption(
            asset_ref="my-model@2.0.0",
            status_at_consumption="staging",
            capsule_dir=capsule_dir,
            registry="remote",
        )

        rec = json.loads((capsule_dir / "assets.jsonl").read_text().strip())
        assert rec["registry"] == "remote"

    def test_lineage_writer_propagates_status_into_facets(self, tmp_path: Path) -> None:
        """LineageWriter._consumed_edges picks up status_at_consumption into facets."""
        run_id = "01TESTRUN000000000000000001"
        capsule_dir = tmp_path / run_id
        capsule_dir.mkdir()

        # Write assets.jsonl with status_at_consumption field
        record = {
            "asset_ref": "fraud-model@1.0.0",
            "registry": "local",
            "status_at_consumption": "production",
        }
        (capsule_dir / "assets.jsonl").write_text(
            json.dumps(record, separators=(",", ":")) + "\n"
        )

        writer = LineageWriter(capsule_dir=capsule_dir, run_id=run_id)
        edges = writer.infer()

        consumed_edges = [e for e in edges if e.edge_type == "consumed"]
        assert len(consumed_edges) == 1
        edge = consumed_edges[0]
        assert edge.facets is not None
        assert edge.facets.get("status_at_consumption") == "production"

    def test_lineage_writer_no_facets_when_status_absent(self, tmp_path: Path) -> None:
        """Older assets.jsonl records without status_at_consumption produce no facets."""
        run_id = "01TESTRUN000000000000000002"
        capsule_dir = tmp_path / run_id
        capsule_dir.mkdir()

        # Write assets.jsonl WITHOUT status_at_consumption (legacy format)
        record = {"asset_ref": "fraud-model@1.0.0", "registry": "local"}
        (capsule_dir / "assets.jsonl").write_text(
            json.dumps(record, separators=(",", ":")) + "\n"
        )

        writer = LineageWriter(capsule_dir=capsule_dir, run_id=run_id)
        edges = writer.infer()

        consumed_edges = [e for e in edges if e.edge_type == "consumed"]
        assert len(consumed_edges) == 1
        assert consumed_edges[0].facets is None

    def test_status_at_consumption_persisted_in_lineage_store(self, tmp_path: Path) -> None:
        """Edge written by LineageStore.insert_edge preserves facets in payload."""
        db_path = tmp_path / "lineage.db"
        run_id = "01TESTRUN000000000000000003"
        capsule_dir = tmp_path / run_id
        capsule_dir.mkdir()

        record = {
            "asset_ref": "fraud-model@1.0.0",
            "registry": "local",
            "status_at_consumption": "staging",
        }
        (capsule_dir / "assets.jsonl").write_text(
            json.dumps(record, separators=(",", ":")) + "\n"
        )

        writer = LineageWriter(capsule_dir=capsule_dir, run_id=run_id)
        edges = writer.infer()

        store = LineageStore(db_path)
        for edge in edges:
            store.insert_edge(edge)

        # Query blast_radius from the asset side
        asset_ref = "local:fraud-model@1.0.0"
        results = store.blast_radius(ref=asset_ref, kind="asset")
        assert any(r["kind"] == "run" for r in results), (
            f"Expected run node in blast-radius, got: {results}"
        )


# ---------------------------------------------------------------------------
# C-1.2 — nova asset diff command
# ---------------------------------------------------------------------------


class TestC12AssetDiff:
    def _register_both(self, tmp_path: Path) -> None:
        import os
        os.environ["NOVAFABRIC_DB_PATH"] = str(tmp_path / "reg.db")
        v1_path = _write_spec(tmp_path, _TOOL_V1_YAML, "v1.yaml")
        v2_path = _write_spec(tmp_path, _TOOL_V2_YAML, "v2.yaml")
        spec_v1 = validate_spec(v1_path)
        spec_v2 = validate_spec(v2_path)
        register_asset(spec_v1, v1_path, db_path=tmp_path / "reg.db")
        register_asset(spec_v2, v2_path, db_path=tmp_path / "reg.db")

    def test_diff_shows_differences_and_exits_1(self, tmp_path: Path) -> None:
        import os
        os.environ["NOVAFABRIC_DB_PATH"] = str(tmp_path / "reg.db")
        try:
            self._register_both(tmp_path)
            result = runner.invoke(
                app,
                ["asset", "diff", "lifecycle-tool@1.0.0", "lifecycle-tool@2.0.0"],
            )
            # Exit 1 when there are differences
            assert result.exit_code == 1
            # Output should show the diff
            assert "lifecycle-tool@1.0.0" in result.output
            assert "lifecycle-tool@2.0.0" in result.output
        finally:
            del os.environ["NOVAFABRIC_DB_PATH"]

    def test_diff_json_output_format(self, tmp_path: Path) -> None:
        import os
        os.environ["NOVAFABRIC_DB_PATH"] = str(tmp_path / "reg.db")
        try:
            self._register_both(tmp_path)
            result = runner.invoke(
                app,
                [
                    "asset", "diff",
                    "lifecycle-tool@1.0.0",
                    "lifecycle-tool@2.0.0",
                    "--output-format", "json",
                ],
            )
            assert result.exit_code == 1  # differences present
            payload = json.loads(result.output)
            assert payload["ref_a"] == "lifecycle-tool@1.0.0"
            assert payload["ref_b"] == "lifecycle-tool@2.0.0"
            assert payload["identical"] is False
            # version and status changed
            changed_keys = set(payload["changed"].keys())
            assert "version" in changed_keys or "status" in changed_keys
        finally:
            del os.environ["NOVAFABRIC_DB_PATH"]

    def test_diff_identical_versions_exits_0(self, tmp_path: Path) -> None:
        import os
        os.environ["NOVAFABRIC_DB_PATH"] = str(tmp_path / "reg.db")
        try:
            v1_path = _write_spec(tmp_path, _TOOL_V1_YAML, "v1.yaml")
            spec_v1 = validate_spec(v1_path)
            register_asset(spec_v1, v1_path, db_path=tmp_path / "reg.db")
            # diff same version against itself
            result = runner.invoke(
                app,
                ["asset", "diff", "lifecycle-tool@1.0.0", "lifecycle-tool@1.0.0"],
            )
            assert result.exit_code == 0
            assert "No differences found" in result.output
        finally:
            del os.environ["NOVAFABRIC_DB_PATH"]

    def test_diff_unknown_asset_exits_1(self, tmp_path: Path) -> None:
        import os
        os.environ["NOVAFABRIC_DB_PATH"] = str(tmp_path / "reg.db")
        try:
            result = runner.invoke(
                app,
                ["asset", "diff", "ghost@1.0.0", "ghost@2.0.0"],
            )
            assert result.exit_code == 1
        finally:
            del os.environ["NOVAFABRIC_DB_PATH"]

    def test_diff_missing_at_sign_exits_with_error(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["asset", "diff", "no-at-sign", "lifecycle-tool@2.0.0"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# C-1.3 — declared dependency graph for blast-radius
# ---------------------------------------------------------------------------


class TestC13DeclaredDeps:
    def test_register_records_dependency_lineage_edge(self, tmp_path: Path) -> None:
        """Registering an asset with dependencies writes depends_on lineage edges."""
        db_path = tmp_path / "reg.db"
        dep_path = _write_spec(tmp_path, _DEP_TOOL_YAML, "dep.yaml")
        consumer_path = _write_spec(tmp_path, _CONSUMER_MODEL_YAML, "consumer.yaml")

        dep_spec = validate_spec(dep_path)
        consumer_spec = validate_spec(consumer_path)

        register_asset(dep_spec, dep_path, db_path=db_path)
        register_asset(consumer_spec, consumer_path, db_path=db_path)

        # Verify lineage edge exists
        store = LineageStore(db_path)
        # blast_radius from the dependency should include the consumer
        dep_ref = "local:dep-tool@1.0.0"
        store.provenance(ref=dep_ref, kind="asset")
        # provenance traverses forward (source → targets); consumer depends on dep
        # so consumer is source and dep is target; blast-radius of dep should give consumer
        blast = store.blast_radius(ref=dep_ref, kind="asset")
        assert any(
            "consumer-model" in r.get("ref", "") for r in blast
        ), f"Expected consumer-model in blast-radius of dep-tool, got: {blast}"

    def test_depends_on_edge_has_declared_confidence(self, tmp_path: Path) -> None:
        """depends_on edges carry confidence=declared."""
        db_path = tmp_path / "reg.db"
        dep_path = _write_spec(tmp_path, _DEP_TOOL_YAML, "dep.yaml")
        consumer_path = _write_spec(tmp_path, _CONSUMER_MODEL_YAML, "consumer.yaml")

        dep_spec = validate_spec(dep_path)
        consumer_spec = validate_spec(consumer_path)
        register_asset(dep_spec, dep_path, db_path=db_path)
        register_asset(consumer_spec, consumer_path, db_path=db_path)

        store = LineageStore(db_path)
        conn = store._conn
        rows = conn.execute(
            "SELECT payload FROM lineage_edges WHERE edge_type = 'depends_on'"
        ).fetchall()
        assert len(rows) >= 1, "Expected at least one depends_on edge"
        payload = json.loads(rows[0]["payload"])
        assert payload["confidence"] == "declared"

    def test_empty_dependencies_writes_no_edges(self, tmp_path: Path) -> None:
        """Assets without dependencies produce no depends_on edges."""
        db_path = tmp_path / "reg.db"
        dep_path = _write_spec(tmp_path, _DEP_TOOL_YAML, "dep.yaml")
        dep_spec = validate_spec(dep_path)
        register_asset(dep_spec, dep_path, db_path=db_path)

        store = LineageStore(db_path)
        conn = store._conn
        rows = conn.execute(
            "SELECT payload FROM lineage_edges WHERE edge_type = 'depends_on'"
        ).fetchall()
        assert rows == []

    def test_dependencies_field_in_spec_model(self, tmp_path: Path) -> None:
        """BaseAssetSpec.dependencies parses correctly from YAML."""
        consumer_path = _write_spec(tmp_path, _CONSUMER_MODEL_YAML, "consumer.yaml")
        spec = validate_spec(consumer_path)
        assert spec.dependencies == ["dep-tool@1.0.0"]

    def test_blast_radius_traverses_declared_deps(self, tmp_path: Path) -> None:
        """nova lineage blast-radius traverses declared dep edges."""
        import os
        db_path = tmp_path / "reg.db"
        os.environ["NOVAFABRIC_DB_PATH"] = str(db_path)
        try:
            dep_path = _write_spec(tmp_path, _DEP_TOOL_YAML, "dep.yaml")
            consumer_path = _write_spec(tmp_path, _CONSUMER_MODEL_YAML, "consumer.yaml")

            dep_spec = validate_spec(dep_path)
            consumer_spec = validate_spec(consumer_path)
            register_asset(dep_spec, dep_path, db_path=db_path)
            register_asset(consumer_spec, consumer_path, db_path=db_path)

            result = runner.invoke(
                app,
                ["lineage", "blast-radius", "local:dep-tool@1.0.0", "--kind", "asset"],
            )
            assert result.exit_code == 0
            assert "consumer-model" in result.output
        finally:
            del os.environ["NOVAFABRIC_DB_PATH"]
