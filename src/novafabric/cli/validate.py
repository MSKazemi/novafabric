from __future__ import annotations

import json
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import typer
import yaml
from rich.console import Console

from novafabric.spec.validator import (
    SpecValidationError,
    print_spec_error,
    validate_spec,
)

console = Console()

SCHEMA_DIR = Path(__file__).parents[2] / "novafabric" / "schemas"

_CAPSULE_SCHEMAS = {
    "capsule.yaml": "run-capsule.schema.json",
    "env.lock": "environment.schema.json",
    "redaction-proof.json": "secret-redaction.schema.json",
}

_REPLAY_SCHEMAS = {
    "replay_result.yaml": "replay-result.schema.json",
}


def _load_schema(name: str) -> dict[str, object]:
    return json.loads((SCHEMA_DIR / name).read_text())  # type: ignore[no-any-return]


def _is_capsule_dir(path: Path) -> bool:
    return path.is_dir() and (path / "capsule.yaml").exists()


def _is_replay_dir(path: Path) -> bool:
    return path.is_dir() and (path / "replay_result.yaml").exists()


def _validate_capsule(capsule_dir: Path) -> None:
    errors: list[str] = []

    for filename, schema_name in _CAPSULE_SCHEMAS.items():
        artifact = capsule_dir / filename
        if not artifact.exists():
            errors.append(f"missing: {filename}")
            continue
        try:
            schema = _load_schema(schema_name)
            if filename.endswith(".json"):
                data = json.loads(artifact.read_text())
            else:
                data = yaml.safe_load(artifact.read_text())
            jsonschema.validate(data, schema, format_checker=jsonschema.FormatChecker())
        except jsonschema.ValidationError as exc:
            errors.append(f"{filename}: {exc.message}")
        except Exception as exc:
            errors.append(f"{filename}: {exc}")

    for fname in ("trace.jsonl", "model-calls.jsonl", "tool-calls.jsonl", "assets.jsonl"):
        if not (capsule_dir / fname).exists():
            errors.append(f"missing: {fname}")

    # Optional: validate lineage.jsonl schema if present (v0.4)
    lineage_path = capsule_dir / "lineage.jsonl"
    if lineage_path.exists():
        lineage_schema = _load_schema("lineage-edge.schema.json")
        for i, line in enumerate(lineage_path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                jsonschema.validate(record, lineage_schema,
                                    format_checker=jsonschema.FormatChecker())
            except json.JSONDecodeError as exc:
                errors.append(f"lineage.jsonl line {i}: invalid JSON: {exc}")
            except jsonschema.ValidationError as exc:
                errors.append(f"lineage.jsonl line {i}: {exc.message}")

    for dname in ("inputs", "outputs"):
        if not (capsule_dir / dname).is_dir():
            errors.append(f"missing directory: {dname}")

    if errors:
        for err in errors:
            console.print(f"[red]✗[/red] {err}")
        raise typer.Exit(code=1)

    manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
    console.print(
        f"[green]✓[/green] Valid capsule: {manifest.get('run_id')}  "
        f"status={manifest.get('status')}"
    )


def _validate_replay(replay_dir: Path) -> None:
    errors: list[str] = []

    for filename, schema_name in _REPLAY_SCHEMAS.items():
        artifact = replay_dir / filename
        if not artifact.exists():
            errors.append(f"missing: {filename}")
            continue
        try:
            schema = _load_schema(schema_name)
            data = yaml.safe_load(artifact.read_text())
            jsonschema.validate(data, schema, format_checker=jsonschema.FormatChecker())
        except jsonschema.ValidationError as exc:
            errors.append(f"{filename}: {exc.message}")
        except Exception as exc:
            errors.append(f"{filename}: {exc}")

    if errors:
        for err in errors:
            console.print(f"[red]✗[/red] {err}")
        raise typer.Exit(code=1)

    result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    console.print(
        f"[green]✓[/green] Valid replay result: {result.get('replay_id')}  "
        f"mode={result.get('mode')}  status={result.get('status')}"
    )


def validate_cmd(
    spec_file: Path = typer.Argument(
        ..., help="Path to asset YAML spec, capsule directory, or replay directory"
    ),
) -> None:
    """Validate an asset YAML spec file without writing to the registry.

    Runs the same schema and field checks as `nova register` but makes no
    changes. Use in CI to gate spec files before merging.

    Scope: single spec file.

    \b
    Examples:
      # Validate a spec
      nova validate assets/my-agent.yaml

      # Use in CI (exits non-zero on failure)
      nova validate assets/my-agent.yaml && echo "Spec is valid"
    """
    if _is_replay_dir(spec_file):
        _validate_replay(spec_file)
        return

    if _is_capsule_dir(spec_file):
        _validate_capsule(spec_file)
        return

    try:
        spec = validate_spec(spec_file)
        console.print(
            f"[green]✓[/green] Valid {spec.asset_type.value} spec: "
            f"{spec.name}@{spec.version}"
        )
    except SpecValidationError as exc:
        print_spec_error(console, spec_file, exc)
        raise typer.Exit(code=1)
