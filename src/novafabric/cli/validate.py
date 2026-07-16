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


def _emit_capsule_validated(capsule_dir: Path, errors: list[str]) -> None:
    """ADR-0137: emit a `capsule.validated` lifecycle event.

    Opt-in via NOVA_EVENTS_* (no-op when unconfigured) and fail-safe —
    emission must never change the validation outcome.
    """
    try:
        from novafabric.events.emitter import emit_lifecycle_event

        ref = capsule_dir.name
        try:
            manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
            if isinstance(manifest, dict) and manifest.get("run_id"):
                ref = str(manifest["run_id"])
        except Exception:  # noqa: BLE001 — best-effort ref resolution
            pass
        emit_lifecycle_event(
            event_type="capsule.validated",
            subject_kind="capsule",
            subject_ref=ref,
            payload={
                "result": "fail" if errors else "pass",
                "error_count": len(errors),
            },
            source="nova validate",
        )
    except Exception:  # noqa: BLE001 — emission must never block validation
        pass


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

    # ADR-0125: integrity-check content-addressed media on model calls —
    # each MediaPart block must validate against media-part.schema.json, its
    # blob (when captured) must exist, and the stored bytes must re-hash to
    # the recorded content_hash. No media blocks ⇒ no-op.
    try:
        from novafabric.capture.media import verify_media_blobs

        errors.extend(verify_media_blobs(capsule_dir))
    except Exception as exc:  # noqa: BLE001 — surface, don't crash validation
        errors.append(f"model-calls.jsonl: media integrity check failed: {exc}")

    _emit_capsule_validated(capsule_dir, errors)

    if errors:
        for err in errors:
            console.print(f"[red]✗[/red] {err}")
        raise typer.Exit(code=1)

    manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())

    # ADR-0126: advisory warning (never an error) for a deployment_environment
    # value outside the conventional set — custom environments are legitimate.
    deployment_environment = manifest.get("deployment_environment")
    if isinstance(deployment_environment, str):
        from novafabric.capture.deployment_env import unconventional_warning

        warning = unconventional_warning(deployment_environment)
        if warning is not None:
            console.print(f"[yellow]⚠[/yellow] {warning}")

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


def _validate_tool_schemas(
    capsule_dir: Path, *, fail_on_violation: bool, write: bool
) -> None:
    """ADR-0128: report tool-call conformance against declared *_schema_ref."""
    from novafabric.capture.schema_validation import (
        validate_capsule_tool_calls,
        write_back_verdicts,
    )

    if write:
        annotated = write_back_verdicts(capsule_dir)
        console.print(
            f"[green]✓[/green] backfilled schema_validation verdicts "
            f"on {annotated} record(s)"
        )

    report = validate_capsule_tool_calls(capsule_dir)
    console.print(f"tool-calls.jsonl: {report.total} records")
    console.print(
        f"  schema-checked   : {report.checked}   "
        f"({report.no_schema} had no schema_ref → null)"
    )
    console.print(
        f"  arguments_valid  : {report.arguments_valid}/{report.arguments_checked}"
    )
    result_line = f"  result_valid     : {report.result_valid}/{report.result_checked}"
    if report.violations:
        result_line += f"   [red]✗[/red] {len(report.violations)} violation(s)"
    console.print(result_line)
    if report.unresolved:
        console.print(
            f"  [yellow]![/yellow] {report.unresolved} record(s) had an "
            "unresolvable schema_ref (verdict null)"
        )
    for violation in report.violations:
        console.print(
            f"  [red]✗[/red] {violation['tool_name']} "
            f"({violation['tool_call_id']}):"
        )
        for err in violation["errors"]:
            console.print(
                f"      {err['target']} {err['path']}: {err['message']}"
            )
    gate = "exit 1 on violation" if fail_on_violation else "report-only; exit 0"
    console.print(
        f"conformance: {report.payloads_valid}/{report.payloads_checked} "
        f"checked payloads valid ({gate})"
    )
    if fail_on_violation and report.violations:
        raise typer.Exit(code=1)


def validate_cmd(
    spec_file: Path = typer.Argument(
        ..., help="Path to asset YAML spec, capsule directory, or replay directory"
    ),
    schemas: bool = typer.Option(
        False,
        "--schemas",
        help="Also validate each tool call's arguments/result against its declared "
        "*_schema_ref JSON Schemas and report conformance (ADR-0128; "
        "capsule directories only; report-only by default).",
    ),
    fail_on_schema_violation: bool = typer.Option(
        False,
        "--fail-on-schema-violation",
        help="With --schemas: exit non-zero if any checked payload violates its "
        "schema (CI gate).",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="With --schemas: persist the computed schema_validation verdicts back "
        "into tool-calls.jsonl (backfill capsules captured before ADR-0128).",
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

      # Tool-call schema conformance report (ADR-0128)
      nova validate --schemas .novafabric/runs/<run-id>/
    """
    if _is_replay_dir(spec_file):
        _validate_replay(spec_file)
        return

    if _is_capsule_dir(spec_file):
        _validate_capsule(spec_file)
        if schemas:
            _validate_tool_schemas(
                spec_file,
                fail_on_violation=fail_on_schema_violation,
                write=write,
            )
        return

    if schemas:
        console.print(
            "[red]✗[/red] --schemas requires a capsule directory "
            "(a directory containing capsule.yaml)"
        )
        raise typer.Exit(code=2)

    try:
        spec = validate_spec(spec_file)
        console.print(
            f"[green]✓[/green] Valid {spec.asset_type.value} spec: "
            f"{spec.name}@{spec.version}"
        )
    except SpecValidationError as exc:
        print_spec_error(console, spec_file, exc)
        raise typer.Exit(code=1)
