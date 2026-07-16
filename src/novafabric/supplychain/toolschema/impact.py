"""Tool-schema replay-impact analysis (ADR-0148 D2 / NF-165).

Given a **new** schema for a tool, re-validate the **historical** captured tool-call payloads
against it and emit a ``schema_impact`` report naming exactly the runs that break (with per-run
failing paths). This answers "if I ship this schema change, which past runs would it reject?".

**Reuse, don't fork (ADR-0148 §D2).** The re-validation runs the shipped ADR-0128 validator core
(:func:`novafabric.capture.schema_validation._check_target`) over the historical payloads — this
module imports it and does **not** reimplement schema validation. The report lists facts (the broken
runs and their failing paths); it is not a gate and carries no promote/pass verdict.

This first slice takes the historical tool-call records directly (each ``{run_id, arguments}``); the
collector that gathers them for a tool across sealed capsules is a documented follow-on.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel

# Reuse the ADR-0128 validator core — never reimplement it (ADR-0148 §D2, tested by import).
from novafabric.capture.schema_validation import _check_target


class BrokenRun(BaseModel):
    run_id: str
    failing_paths: list[str]  # the json paths that fail under the new schema


class SchemaImpactReport(BaseModel):
    tool_id: str
    new_schema_digest: str  # sha256 of the new schema file
    broken_run_ids: list[BrokenRun]
    checked: int  # how many historical records were re-validated
    # Intentionally NO verdict/passed/gate/promote field — it reports impact, it does not gate.


def _schema_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def compute_schema_impact(
    *,
    tool_id: str,
    new_schema_path: Path,
    tool_calls: Sequence[Mapping[str, object]],
    target: str = "arguments",
) -> SchemaImpactReport:
    """Re-validate each historical tool-call's ``target`` payload against the new schema.

    Only records that carry the ``target`` key (``arguments`` by default) are checked. A record
    whose payload violates the new schema is listed in ``broken_run_ids`` with its failing paths.
    Raises ``ValueError`` if the new schema file is missing/unreadable.
    """
    if not new_schema_path.is_file():
        raise ValueError(f"new schema file not found: {new_schema_path}")
    digest = _schema_digest(new_schema_path)
    ref = str(new_schema_path.resolve())
    base_dir = new_schema_path.resolve().parent

    broken: list[BrokenRun] = []
    checked = 0
    for record in tool_calls:
        if target not in record:
            continue
        checked += 1
        errors: list[dict[str, object]] = []
        result = _check_target(record.get(target), ref, target, base_dir, errors)
        if result is False:  # violations recorded (None = schema unresolved, True = conforms)
            run_id = str(record.get("run_id", ""))
            failing = sorted({str(e.get("path", "$")) for e in errors})
            broken.append(BrokenRun(run_id=run_id, failing_paths=failing))

    return SchemaImpactReport(
        tool_id=tool_id,
        new_schema_digest=digest,
        broken_run_ids=broken,
        checked=checked,
    )
