"""Capsule format migration: v0.1.x → v1.0.0.

Per ADR-0034 §6: the migrator copies the source capsule to the output path,
bumps all schema_version fields, and appends a migration_source lineage edge.
The source directory is never modified.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from novafabric.capture._ulid import new_ulid
from novafabric.lineage._types import LineageEdge

SOURCE_VERSION = "0.1.0"
TARGET_VERSION = "1.0.0"

# YAML files inside a capsule directory that may carry schema_version
_YAML_FILES = frozenset({"capsule.yaml", "replay.yaml", "env.lock"})

# JSON files inside a capsule directory that may carry schema_version
_JSON_FILES = frozenset({"redaction-proof.json", "manifest.json"})


@dataclass
class MigrationResult:
    source: Path
    output: Path
    files_updated: list[str]
    lineage_edge_id: str


class CapsuleMigrationError(Exception):
    """Structured error from capsule migration.

    Carries field, rule, and spec_section so CLI callers can emit
    machine-readable errors via --json-errors (ADR-0034 §6).
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        rule: str | None = None,
        spec_section: str | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.rule = rule
        self.spec_section = spec_section

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"error": str(self)}
        if self.field is not None:
            d["field"] = self.field
        if self.rule is not None:
            d["rule"] = self.rule
        if self.spec_section is not None:
            d["spec_section"] = self.spec_section
        return d


class CapsuleMigrator:
    """Converts a v0.1.x capsule directory to v1.0.0.

    Usage::

        result = CapsuleMigrator().migrate(source=Path("my-run"), output=Path("my-run-v1"))
    """

    def migrate(self, source: Path, output: Path) -> MigrationResult:
        """Copy *source* to *output* and upgrade all schema_version fields."""
        self._validate_source(source)

        if output.exists():
            raise CapsuleMigrationError(
                f"Output path already exists: {output}",
                rule="output must not exist before migration",
                spec_section="ADR-0034 §6",
            )

        shutil.copytree(source, output)

        files_updated: list[str] = []

        for name in _YAML_FILES:
            path = output / name
            if path.exists() and self._bump_yaml_version(path):
                files_updated.append(name)

        for name in _JSON_FILES:
            path = output / name
            if path.exists() and self._bump_json_version(path):
                files_updated.append(name)

        for jsonl_path in sorted(output.glob("*.jsonl")):
            if self._bump_jsonl_versions(jsonl_path):
                files_updated.append(jsonl_path.name)

        edge_id = self._write_migration_edge(source, output)

        return MigrationResult(
            source=source,
            output=output,
            files_updated=files_updated,
            lineage_edge_id=edge_id,
        )

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_source(self, source: Path) -> None:
        if not source.exists():
            raise CapsuleMigrationError(
                f"Source capsule not found: {source}",
                rule="source path must exist",
                spec_section="ADR-0034 §6",
            )
        if not source.is_dir():
            raise CapsuleMigrationError(
                f"Source must be a capsule directory (tarballs not yet supported): {source}",
                rule="source must be a directory",
                spec_section="ADR-0034 §6",
            )
        capsule_yaml = source / "capsule.yaml"
        if not capsule_yaml.exists():
            raise CapsuleMigrationError(
                f"No capsule.yaml found in {source}",
                field="capsule.yaml",
                rule="capsule directory must contain capsule.yaml",
                spec_section="Run Capsule v1 spec §Structure",
            )
        with open(capsule_yaml) as f:
            data = yaml.safe_load(f)
        version = (data or {}).get("schema_version", "")
        if version == TARGET_VERSION:
            raise CapsuleMigrationError(
                f"Capsule is already at schema_version {TARGET_VERSION}",
                field="capsule.yaml/schema_version",
                rule="source schema_version must be 0.1.x",
                spec_section="ADR-0034 §6",
            )
        if not str(version).startswith("0.1."):
            raise CapsuleMigrationError(
                f"Unsupported source schema_version '{version}': expected 0.1.x",
                field="capsule.yaml/schema_version",
                rule="source schema_version must be 0.1.x",
                spec_section="ADR-0034 §6",
            )

    # ── Per-format bumpers ────────────────────────────────────────────────────

    def _bump_yaml_version(self, path: Path) -> bool:
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return False
        if data.get("schema_version") != SOURCE_VERSION:
            return False
        data["schema_version"] = TARGET_VERSION
        with open(path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return True

    def _bump_json_version(self, path: Path) -> bool:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        if data.get("schema_version") != SOURCE_VERSION:
            return False
        data["schema_version"] = TARGET_VERSION
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        return True

    def _bump_jsonl_versions(self, path: Path) -> bool:
        raw = path.read_text()
        lines = raw.splitlines()
        updated = False
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                new_lines.append(line)
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if isinstance(record, dict) and record.get("schema_version") == SOURCE_VERSION:
                record["schema_version"] = TARGET_VERSION
                new_lines.append(json.dumps(record, separators=(",", ":")))
                updated = True
            else:
                new_lines.append(line)
        if updated:
            content = "\n".join(new_lines)
            if content and not content.endswith("\n"):
                content += "\n"
            path.write_text(content)
        return updated

    # ── Lineage edge ──────────────────────────────────────────────────────────

    def _write_migration_edge(self, source: Path, output: Path) -> str:
        capsule_yaml = output / "capsule.yaml"
        with open(capsule_yaml) as f:
            capsule = yaml.safe_load(f)
        run_id: str = (capsule or {}).get("run_id", new_ulid())

        edge = LineageEdge(
            edge_type="migration_source",
            source={
                "kind": "capsule",
                "ref": str(source.resolve()),
                "schema_version": SOURCE_VERSION,
            },
            target={
                "kind": "capsule",
                "ref": str(output.resolve()),
                "schema_version": TARGET_VERSION,
            },
            confidence="high",
            capsule_run_id=run_id,
            facets={"from_schema_version": SOURCE_VERSION},
        )

        lineage_path = output / "lineage.jsonl"
        with open(lineage_path, "a") as f:
            f.write(json.dumps(edge.as_dict(), separators=(",", ":")) + "\n")

        # Ensure capsule.yaml references lineage.jsonl
        if not (capsule or {}).get("lineage_ref"):
            capsule["lineage_ref"] = "lineage.jsonl"
            with open(capsule_yaml, "w") as f:
                yaml.safe_dump(
                    capsule, f, default_flow_style=False, sort_keys=False, allow_unicode=True
                )

        return edge.edge_id
