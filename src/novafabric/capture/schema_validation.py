"""Tool-call schema validation — the ADR-0128 record-only enforcement pass.

The tool-call format has always *declared* where a tool's contract lives
(`arguments_schema_ref` / `result_schema_ref`, see
``the private design/spec/tool-call-v1.md``) without enforcing it. This module turns a
present schema_ref into a recorded ``schema_validation`` verdict block
(``the private design/spec/toolcall-schema-validation-v0.md``):

- **Record-only.** A validation failure is recorded in the verdict's
  ``errors[]`` and is NEVER raised into the captured application.
- **Local-only resolution.** Relative refs resolve inside the capsule
  directory, read without following a link (:mod:`novafabric._capsule_fs`).
  Absolute local paths are allowed **at capture only**, where the recorded
  application declares its own schema. Re-validating a stored capsule
  (``nova validate --tool-schemas``, replay drift) is reading input someone
  else wrote: there an absolute ref resolves only inside NovaFabric's packaged
  ``schemas/`` directory and is otherwise ``schema-unresolved``, so a capsule
  cannot make the reader parse an arbitrary local file. ``http(s)://`` refs are never
  fetched (offline rule) and are recorded as ``schema-unresolved``. The same
  rule holds for ``$ref`` / ``$dynamicRef`` *inside* a loaded schema: only
  refs within the schema document and the bundled JSON Schema meta-schemas
  resolve (:mod:`novafabric._jsonschema_offline`); a ref to any other URI —
  ``http(s)://``, ``file://``, or relative to a remote ``$id`` — is never
  retrieved, and the target's verdict is ``null`` with one
  ``schema-unresolved`` error (no partial violations are kept).
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

from novafabric._capsule_fs import (
    CapsuleEntryError,
    CapsuleFileTooLargeError,
    open_capsule_file_for_write,
    read_capsule_file,
    read_capsule_text_or_none,
)

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


def _packaged_schema_dir() -> Path:
    return (Path(__file__).resolve().parents[1] / "schemas").resolve()


def _read_relative(ref: str, base_dir: Path) -> tuple[bytes | None, str | None]:
    """Read *ref* inside *base_dir*: no ``..``, no link, no FIFO, size-capped."""
    try:
        raw = read_capsule_file(
            base_dir, ref, max_bytes=MAX_SCHEMA_BYTES, operation="schema validation"
        )
    except ValueError:
        return None, f"relative schema_ref escapes the capsule directory: {ref}"
    except CapsuleFileTooLargeError:
        return None, f"schema file exceeds {MAX_SCHEMA_BYTES} bytes: {ref}"
    except CapsuleEntryError:
        return None, f"schema_ref is a link or not a regular file (never followed): {ref}"
    except FileNotFoundError:
        return None, f"schema file not found: {ref}"
    except OSError as exc:
        return None, f"schema unreadable ({type(exc).__name__}): {ref}"
    return raw, None


def _read_absolute(path: Path, ref: str, *, trusted: bool) -> tuple[bytes | None, str | None]:
    """Read an absolute *ref*: anywhere when *trusted* (capture), else packaged schemas only."""
    if not trusted:
        resolved = path.resolve()
        if ".." in path.parts or not resolved.is_relative_to(_packaged_schema_dir()):
            return None, (
                "absolute schema_ref is not read when re-validating a stored capsule "
                f"(only the capsule and the packaged schemas are): {ref}"
            )
        path = resolved
    try:
        if not path.is_file():
            return None, f"schema file not found: {ref}"
        if path.stat().st_size > MAX_SCHEMA_BYTES:
            return None, f"schema file exceeds {MAX_SCHEMA_BYTES} bytes: {ref}"
        return path.read_bytes(), None
    except OSError as exc:
        return None, f"schema unreadable ({type(exc).__name__}): {ref}"


def _load_schema(ref: str, base_dir: Path, *, trusted: bool = True) -> tuple[Any, str | None]:
    """Resolve *ref* to a parsed JSON Schema. Local-only; never the network.

    *trusted* is True at capture (the recorded application declared the ref)
    and False when re-validating a stored capsule; see the module docstring.
    Returns ``(schema, None)`` on success or ``(None, reason)`` when the ref
    cannot be resolved. Never raises.
    """
    if ref.startswith(("http://", "https://")):
        return None, f"network schema refs are not fetched (offline-only): {ref}"
    path = Path(ref)
    if path.is_absolute():
        raw, problem = _read_absolute(path, ref, trusted=trusted)
    else:
        raw, problem = _read_relative(ref, base_dir)
    if raw is None:
        return None, problem
    try:
        schema = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
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
    *,
    trusted: bool = True,
) -> bool | None:
    """Validate *payload* against the schema at *ref*, appending SchemaErrors.

    Returns True (conforms), False (violations recorded), or None
    (schema unresolved — recorded with keyword ``schema-unresolved``).
    Never raises.
    """
    schema, problem = _load_schema(ref, base_dir, trusted=trusted)
    if schema is None:
        errors.append({
            "target": target,
            "path": "$",
            "message": _sanitize(problem or "schema unresolved"),
            "keyword": _UNRESOLVED_KEYWORD,
        })
        return None
    # Collected locally and merged only on a completed pass: a pass aborted by
    # an unresolved $ref is a ``null`` verdict and must not carry the half-set
    # of violations found before the ref was reached.
    found: list[dict[str, Any]] = []
    try:
        import jsonschema  # type: ignore[import-untyped]

        from novafabric._jsonschema_offline import SchemaRefUnresolved, offline_validator

        # Never retrieves a $ref: only refs inside the schema document and the
        # bundled meta-schemas resolve (offline rule — no http(s)://, no file://).
        validator = offline_validator(schema, format_checker=jsonschema.FormatChecker())
        truncated = False
        for err in validator.iter_errors(payload):
            if len(errors) + len(found) >= MAX_ERRORS:
                truncated = True
                break
            found.append({
                "target": target,
                "path": str(err.json_path),
                "message": _sanitize(err.message),
                "keyword": str(err.validator) if err.validator else "unknown",
            })
    except SchemaRefUnresolved as exc:
        errors.append({
            "target": target,
            "path": "$",
            "message": _sanitize(
                f"$ref not retrieved (offline-only; refs outside the schema "
                f"document are never fetched): {exc}"
            ),
            "keyword": _UNRESOLVED_KEYWORD,
        })
        return None
    except Exception as exc:  # noqa: BLE001 — record-only; never raise
        errors.append({
            "target": target,
            "path": "$",
            "message": _sanitize(f"validation aborted ({type(exc).__name__}): {exc}"),
            "keyword": _UNRESOLVED_KEYWORD,
        })
        return None
    errors.extend(found)
    if truncated:
        errors.append({
            "target": target,
            "path": "$",
            "message": f"error list truncated at {MAX_ERRORS}",
            "keyword": "truncated",
        })
    return not found and not truncated


def compute_verdict(
    record: dict[str, Any], schema_base_dir: Path, *, trusted: bool = True
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
            record.get("arguments"), args_ref, "arguments", schema_base_dir, errors,
            trusted=trusted,
        )
    if result_ref is not None and record.get("status", "success") == "success":
        result_valid = _check_target(
            record.get("result"), result_ref, "result", schema_base_dir, errors,
            trusted=trusted,
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
    records: list[dict[str, Any]] = []
    text = read_capsule_text_or_none(
        capsule_dir, "tool-calls.jsonl", operation="schema validation"
    )
    if text is None:
        return records
    for line in text.splitlines():
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
        verdict = compute_verdict(record, capsule_dir, trusted=False)
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

    Only a line that gains a verdict is rewritten; every other line — a record
    without a schema_ref, a truncated line, a non-object line — is kept
    byte-for-byte, and the file is not written at all when no record was
    annotated (review capture-runners-17: the backfill used to rewrite the file
    from the parsed records alone, deleting every line it could not parse).
    Returns the number of records annotated (existing verdicts are recomputed
    and replaced).

    Raises:
        SealedCapsuleError: the capsule is NovaSeal-sealed; its tool calls are
            covered by ``evidence_digests`` and are never rewritten in place.
        CapsuleLinkError: ``tool-calls.jsonl`` is a symlink at write time; it
            is never written through.
    """
    from novafabric.capsule._manifest_write import SealedCapsuleError, is_sealed

    if is_sealed(capsule_dir):
        raise SealedCapsuleError(
            "the capsule is sealed: backfilling verdicts would change tool-calls.jsonl, "
            "which its seal covers, and `nova verify` would then fail. "
            "Run `nova validate --schemas` without --write for the report."
        )
    text = read_capsule_text_or_none(
        capsule_dir, "tool-calls.jsonl", operation="nova validate --write"
    )
    if text is None:
        return 0
    annotated = 0
    lines: list[str] = []
    for line in text.splitlines():
        try:
            record = json.loads(line) if line.strip() else None
        except json.JSONDecodeError:
            record = None
        verdict = (
            compute_verdict(record, capsule_dir, trusted=False)
            if isinstance(record, dict) else None
        )
        if verdict is None:
            lines.append(line)
            continue
        assert isinstance(record, dict)  # a verdict is computed only for an object
        record["schema_validation"] = verdict
        annotated += 1
        lines.append(json.dumps(record, separators=(",", ":")))
    if annotated:
        # Never through a link, even one planted after the read above.
        with open_capsule_file_for_write(
            capsule_dir, "tool-calls.jsonl", mode="w", operation="nova validate --write"
        ) as fh:
            fh.write(("\n".join(lines) + "\n").encode("utf-8"))
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
            verdict = compute_verdict(record, capsule_dir, trusted=False)
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
