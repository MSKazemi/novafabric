"""Tests that all OAS v1 JSON schemas lock schema_version to ^1\\. (V-2, ADR-0034 §1)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

_SCHEMAS_DIR = Path(__file__).parents[1] / "schemas"

# Schemas that carry a schema_version property (diff-report does not).
_VERSION_SCHEMAS = [
    "run-capsule.schema.json",
    "evidence-bundle.schema.json",
    "model-call.schema.json",
    "tool-call.schema.json",
    "lineage-edge.schema.json",
    "environment.schema.json",
    "replay-policy.schema.json",
    "secret-redaction.schema.json",
]


def _load(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / name).read_text())


def _version_sub(schema: dict) -> dict:
    """Extract the schema_version property sub-schema for targeted pattern testing."""
    return schema["properties"]["schema_version"]


def _check_version(sub_schema: dict, value: str) -> list[str]:
    """Validate a version string against the schema_version sub-schema."""
    return [
        e.message
        for e in jsonschema.Draft7Validator(sub_schema).iter_errors(value)
    ]


@pytest.mark.parametrize("schema_name", _VERSION_SCHEMAS)
class TestSchemaVersionLock:
    def test_version_100_passes(self, schema_name: str) -> None:
        """schema_version '1.0.0' must be accepted by the v1 schema."""
        sub = _version_sub(_load(schema_name))
        errors = _check_version(sub, "1.0.0")
        assert errors == [], f"{schema_name}: unexpected errors with 1.0.0: {errors}"

    def test_version_110_passes(self, schema_name: str) -> None:
        """schema_version '1.1.0' must be accepted — minor bumps within 1.x.x are valid."""
        sub = _version_sub(_load(schema_name))
        errors = _check_version(sub, "1.1.0")
        assert errors == [], f"{schema_name}: unexpected errors with 1.1.0: {errors}"

    def test_version_010_fails(self, schema_name: str) -> None:
        """schema_version '0.1.0' must be rejected (ADR-0034 §1)."""
        sub = _version_sub(_load(schema_name))
        errors = _check_version(sub, "0.1.0")
        assert errors, (
            f"{schema_name}: expected validation error for schema_version=0.1.0 "
            f"but got none — pattern not locked to ^1\\."
        )

    def test_version_200_fails(self, schema_name: str) -> None:
        """schema_version '2.0.0' must be rejected — locked to 1.x.x."""
        sub = _version_sub(_load(schema_name))
        errors = _check_version(sub, "2.0.0")
        assert errors, (
            f"{schema_name}: expected validation error for schema_version=2.0.0 "
            f"but got none — pattern allows future major versions"
        )

    def test_schema_comment_declares_100(self, schema_name: str) -> None:
        """$comment must contain 'schema_version 1.0.0' (ADR-0034 §1 condition 1)."""
        schema = _load(schema_name)
        assert "schema_version 1.0.0" in schema.get("$comment", ""), (
            f"{schema_name}: $comment does not declare schema_version 1.0.0"
        )


class TestMigrateRoundTrip:
    """End-to-end: migrate a 0.1.x capsule → check schema_version is promoted to 1.0.0."""

    def test_migrated_capsule_has_v1_schema_version(self, tmp_path: Path) -> None:
        import yaml

        from novafabric.capsule_migration._converter import CapsuleMigrator

        src = tmp_path / "capsule_v0"
        src.mkdir()
        # Minimal capsule.yaml — just enough for the migrator to read schema_version
        (src / "capsule.yaml").write_text(yaml.dump({"schema_version": "0.1.0"}))

        out = tmp_path / "capsule_v1"
        migrator = CapsuleMigrator()
        migrator.migrate(source=src, output=out)

        migrated = yaml.safe_load((out / "capsule.yaml").read_text())
        assert migrated["schema_version"] == "1.0.0"

        # Also confirm the v1 locked pattern accepts the migrated value
        schema = _load("run-capsule.schema.json")
        sub = _version_sub(schema)
        errors = _check_version(sub, migrated["schema_version"])
        assert errors == [], (
            f"Migrated schema_version '{migrated['schema_version']}' "
            f"rejected by locked pattern: {errors}"
        )
