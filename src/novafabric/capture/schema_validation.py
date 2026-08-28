"""Tool-call schema validation — the ADR-0128 record-only enforcement pass.

The tool-call format has always *declared* where a tool's contract lives
(`arguments_schema_ref` / `result_schema_ref`, see
``the private design/spec/tool-call-v1.md``) without enforcing it. This module turns a
present schema_ref into a recorded ``schema_validation`` verdict block
(``the private design/spec/toolcall-schema-validation-v0.md``):

- **Record-only.** A validation failure is recorded in the verdict's
  ``errors[]`` and is NEVER raised into the captured application.
- **Local-only resolution.** Relative refs resolve inside the capsule
  directory; absolute local paths are allowed; ``http(s)://`` refs are never
  fetched (offline rule) and are recorded as ``schema-unresolved``.
- **Bounded.** ``errors[]`` is capped (default 50) with a synthetic
  ``truncated`` entry; schema files are size-capped; messages are
  secret-sanitized before entering the capsule.
- **Three-valued.** ``arguments_valid`` / ``result_valid`` are ``true``,
  ``false``, or ``null`` ("no schema declared" / "could not be checked") —
  ``null`` is never a failure.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALIDATOR_ID = "jsonschema/2020-12"
MAX_ERRORS = 50
MAX_SCHEMA_BYTES = 1_048_576  # 1 MiB per referenced schema file
_MAX_MESSAGE_CHARS = 500
_UNRESOLVED_KEYWORD = "schema-unresolved"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sanitize(message: str) -> str:
    """Secret-sanitize an error message before it enters the capsule."""
    from novafabric.otel.content_bridge import redact_text

    return redact_text(message)[:_MAX_MESSAGE_CHARS]


def _load_schema(ref: str, base_dir: Path) -> tuple[Any, str | None]:
    """Resolve *ref* to a parsed JSON Schema. Local-only; never the network.

    Returns ``(schema, None)`` on success or ``(None, reason)`` when the ref
    cannot be resolved. Never raises.
    """
    if ref.startswith(("http://", "https://")):
        return None, f"network schema refs are not fetched (offline-only): {ref}"
    path = Path(ref)
    if not path.is_absolute():
        path = (base_dir / ref).resolve()
        try:
            path.relative_to(base_dir.resolve())
        except ValueError:
            return None, f"relative schema_ref escapes the capsule directory: {ref}"
    try:
        if not path.is_file():
            return None, f"schema file not found: {ref}"
        if path.stat().st_size > MAX_SCHEMA_BYTES:
            return None, f"schema file exceeds {MAX_SCHEMA_BYTES} bytes: {ref}"
        schema = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"schema unreadable ({type(exc).__name__}): {ref}"
    if not isinstance(schema, (dict, bool)):
        return None, f"schema is not a JSON Schema object: {ref}"
    return schema, None


def _check_target(
    payload: Any,
    ref: str,
    target: str,
    base_dir: Path,
    errors: list[dict[str, Any]],
) -> bool | None:
    """Validate *payload* against the schema at *ref*, appending SchemaErrors.

    Returns True (conforms), False (violations recorded), or None
    (schema unresolved — recorded with keyword ``schema-unresolved``).
    Never raises.
    """
    schema, problem = _load_schema(ref, base_dir)
    if schema is None:
        errors.append({
            "target": target,
            "path": "$",
            "message": _sanitize(problem or "schema unresolved"),
            "keyword": _UNRESOLVED_KEYWORD,
        })
        return None
    try:
        import jsonschema  # type: ignore[import-untyped]

        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        found = False
        truncated = False
        for err in validator.iter_errors(payload):
            found = True
            if len(errors) >= MAX_ERRORS:
                truncated = True
                break
            errors.append({
                "target": target,
                "path": str(err.json_path),
                "message": _sanitize(err.message),
                "keyword": str(err.validator) if err.validator else "unknown",
            })
        if truncated:
            errors.append({
                "target": target,
                "path": "$",
                "message": f"error list truncated at {MAX_ERRORS}",
                "keyword": "truncated",
            })
        return not found
    except Exception as exc:  # noqa: BLE001 — record-only; never raise
        errors.append({
            "target": target,
            "path": "$",
            "message": _sanitize(f"validation aborted ({type(exc).__name__}): {exc}"),
            "keyword": _UNRESOLVED_KEYWORD,
        })
        return None


def compute_verdict(
    record: dict[str, Any], schema_base_dir: Path
) -> dict[str, Any] | None:
    """Compute the ``schema_validation`` verdict block for a tool-call record.

    Returns ``None`` when the record declares no schema at all — today's
    behavior is then byte-identical (no block is attached). When the tool
    errored (``status != success``), validation of ``result`` is skipped and
    ``result_valid`` is ``None`` per the spec. Never raises.
    """
    raw_args_ref = record.get("arguments_schema_ref")
    raw_result_ref = record.get("result_schema_ref")
    args_ref: str | None = (
        raw_args_ref if isinstance(raw_args_ref, str) and raw_args_ref else None
    )
    result_ref: str | None = (
        raw_result_ref if isinstance(raw_result_ref, str) and raw_result_ref else None
    )
    if args_ref is None and result_ref is None:
        return None

    errors: list[dict[str, Any]] = []
    arguments_valid: bool | None = None
    result_valid: bool | None = None
    if args_ref is not None:
        arguments_valid = _check_target(
            record.get("arguments"), args_ref, "arguments", schema_base_dir, errors
        )
    if result_ref is not None and record.get("status", "success") == "success":
        result_valid = _check_target(
            record.get("result"), result_ref, "result", schema_base_dir, errors
        )
    return {
        "arguments_valid": arguments_valid,
        "result_valid": result_valid,
        "validator": VALIDATOR_ID,
        "errors": errors,
        "checked_at": _now(),
        "arguments_schema_ref": args_ref,
        "result_schema_ref": result_ref,
    }


def annotate_tool_call(record: dict[str, Any], schema_base_dir: Path) -> None:
    """Attach a ``schema_validation`` verdict to *record* in place.

    No-op when no schema_ref is declared or a verdict is already present.
    Never raises — capture must never block or lose the workload's record.
    """
    try:
        if "schema_validation" in record:
            return
        verdict = compute_verdict(record, schema_base_dir)
        if verdict is not None:
            record["schema_validation"] = verdict
    except Exception:  # noqa: BLE001 — record-only; never raise into capture
        return


def _is_violation(verdict: dict[str, Any] | None) -> bool:
    return verdict is not None and (
        verdict.get("arguments_valid") is False or verdict.get("result_valid") is False
    )


@dataclass
class CapsuleToolSchemaReport:
    """Conformance summary over a capsule's tool-calls.jsonl (ADR-0128 P3)."""

    total: int = 0
    no_schema: int = 0
    arguments_checked: int = 0
    arguments_valid: int = 0
    result_checked: int = 0
    result_valid: int = 0
    unresolved: int = 0
    violations: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any] | None] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return self.total - self.no_schema

    @property
    def payloads_checked(self) -> int:
        return self.arguments_checked + self.result_checked

    @property
    def payloads_valid(self) -> int:
        return self.arguments_valid + self.result_valid


def _read_tool_calls(capsule_dir: Path) -> list[dict[str, Any]]:
    path = capsule_dir / "tool-calls.jsonl"
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def validate_capsule_tool_calls(capsule_dir: Path) -> CapsuleToolSchemaReport:
    """Re-validate every tool call in a capsule against its declared schemas."""
    report = CapsuleToolSchemaReport()
    for record in _read_tool_calls(capsule_dir):
        report.total += 1
        verdict = compute_verdict(record, capsule_dir)
        report.verdicts.append(verdict)
        if verdict is None:
            report.no_schema += 1
            continue
        for target_field, checked_attr, valid_attr in (
            ("arguments_valid", "arguments_checked", "arguments_valid"),
            ("result_valid", "result_checked", "result_valid"),
        ):
            value = verdict[target_field]
            if value is not None:
                setattr(report, checked_attr, getattr(report, checked_attr) + 1)
                if value:
                    setattr(report, valid_attr, getattr(report, valid_attr) + 1)
        if any(e.get("keyword") == _UNRESOLVED_KEYWORD for e in verdict["errors"]):
            report.unresolved += 1
        if _is_violation(verdict):
            report.violations.append({
                "tool_call_id": record.get("tool_call_id"),
                "tool_name": record.get("tool_name"),
                "arguments_valid": verdict["arguments_valid"],
                "result_valid": verdict["result_valid"],
                "errors": verdict["errors"],
            })
    return report


def write_back_verdicts(capsule_dir: Path) -> int:
    """Persist computed verdicts into tool-calls.jsonl (``--write`` backfill).

    Records without a schema_ref are rewritten unchanged. Returns the number
    of records annotated (existing verdicts are recomputed and replaced).
    """
    records = _read_tool_calls(capsule_dir)
    annotated = 0
    lines: list[str] = []
    for record in records:
        verdict = compute_verdict(record, capsule_dir)
        if verdict is not None:
            record["schema_validation"] = verdict
            annotated += 1
        lines.append(json.dumps(record, separators=(",", ":")))
    if records:
        (capsule_dir / "tool-calls.jsonl").write_text("\n".join(lines) + "\n")
    return annotated


def revalidate_tool_calls(
    tool_calls: list[dict[str, Any]], capsule_dir: Path
) -> list[dict[str, Any]]:
    """Replay-time re-validation: return drift findings (ADR-0128 D2/P4).

    A drift finding is a stored tool call whose ``arguments``/``result`` no
    longer conform to its *current* referenced schema. Unresolved schemas are
    ``null`` verdicts, not drift. Never raises.
    """
    drift: list[dict[str, Any]] = []
    for record in tool_calls:
        try:
            verdict = compute_verdict(record, capsule_dir)
        except Exception:  # noqa: BLE001 — replay surfacing must not crash replay
            continue
        if _is_violation(verdict):
            assert verdict is not None
            drift.append({
                "tool_call_id": record.get("tool_call_id"),
                "tool_name": record.get("tool_name"),
                "arguments_valid": verdict["arguments_valid"],
                "result_valid": verdict["result_valid"],
                "errors": verdict["errors"],
            })
    return drift
