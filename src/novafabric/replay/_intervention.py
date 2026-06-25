# src/novafabric/replay/_intervention.py
"""Intervention (counterfactual) replay — ADR-0086, gap-005. Experimental.

The caller supplies an :class:`InterventionSpec` — one target selector plus
exactly one substitution — and the engine replays the capsule with the
substitution applied, re-executing downstream steps under mocked-replay
semantics (ADR-0012 fully inherited).  The output is a minimal Run Capsule
marked ``replay_mode: intervention`` so it can never be mistaken for an
original run, and it is diffable with the existing structural Diff.

Optional check-functions are named per-step assertions evaluated against the
post-substitution event streams; failures are reported by name and never
silently swallowed; ``fatal: true`` aborts the replay.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class InterventionError(Exception):
    """Base class for intervention replay errors."""


class InterventionSpecError(InterventionError):
    """Raised when the intervention spec file is missing or malformed."""


class InterventionTargetNotFoundError(InterventionError):
    """Raised when the target selector matches no captured event."""


class InterventionTarget(BaseModel):
    """Selects exactly one captured event by index or span id."""

    stream: Literal["model-calls", "tool-calls"] = "model-calls"
    event_index: int | None = None
    span_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> "InterventionTarget":
        if (self.event_index is None) == (self.span_id is None):
            raise ValueError(
                "target must set exactly one of event_index or span_id"
            )
        return self


class CheckFunction(BaseModel):
    """A named per-step assertion over the post-substitution event streams."""

    name: str
    stream: Literal["model-calls", "tool-calls"] = "model-calls"
    event_index: int
    field: str = Field(..., description="Dotted path into the event record")
    equals: Any | None = None
    contains: str | None = None
    fatal: bool = False

    @model_validator(mode="after")
    def _exactly_one_assertion(self) -> "CheckFunction":
        if (self.equals is None) == (self.contains is None):
            raise ValueError("check must set exactly one of equals or contains")
        return self


class CheckOutcome(BaseModel):
    """Result of one check-function evaluation — reported, never swallowed."""

    name: str
    passed: bool
    detail: str


class InterventionSpec(BaseModel):
    """One target + exactly one substitution (+ optional checks)."""

    target: InterventionTarget
    replace_model_response: Any | None = None
    replace_tool_result: Any | None = None
    mutate_payload: dict[str, Any] | None = None
    checks: list[CheckFunction] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_substitution(self) -> "InterventionSpec":
        set_count = sum(
            1
            for v in (
                self.replace_model_response,
                self.replace_tool_result,
                self.mutate_payload,
            )
            if v is not None
        )
        if set_count != 1:
            raise ValueError(
                "spec must set exactly one of replace_model_response, "
                "replace_tool_result, mutate_payload"
            )
        if (
            self.replace_tool_result is not None
            and self.target.stream != "tool-calls"
        ):
            raise ValueError("replace_tool_result requires target.stream=tool-calls")
        if (
            self.replace_model_response is not None
            and self.target.stream != "model-calls"
        ):
            raise ValueError(
                "replace_model_response requires target.stream=model-calls"
            )
        return self

    def describe(self) -> dict[str, Any]:
        """Metadata block recorded in the intervention output capsule."""
        substitution = (
            "replace_model_response"
            if self.replace_model_response is not None
            else "replace_tool_result"
            if self.replace_tool_result is not None
            else "mutate_payload"
        )
        return {
            "stream": self.target.stream,
            "selector": (
                {"event_index": self.target.event_index}
                if self.target.event_index is not None
                else {"span_id": self.target.span_id}
            ),
            "substitution": substitution,
            "check_count": len(self.checks),
        }


def load_intervention_spec(path: Path | None) -> InterventionSpec:
    """Load and validate a spec file; all failures are named errors."""
    if path is None:
        raise InterventionSpecError(
            "intervention mode requires --intervention-file"
        )
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise InterventionSpecError(f"cannot read spec file: {exc}") from exc
    except yaml.YAMLError as exc:
        raise InterventionSpecError(f"spec file is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise InterventionSpecError("spec file must contain a YAML mapping")
    try:
        return InterventionSpec.model_validate(raw)
    except ValueError as exc:
        raise InterventionSpecError(f"invalid intervention spec: {exc}") from exc


def _find_target_index(
    spec: InterventionSpec, events: list[dict[str, Any]]
) -> int:
    target = spec.target
    if target.event_index is not None:
        if 0 <= target.event_index < len(events):
            return target.event_index
        raise InterventionTargetNotFoundError(
            f"event_index {target.event_index} out of range for "
            f"{target.stream} (stream has {len(events)} events)"
        )
    for i, record in enumerate(events):
        if record.get("span_id") == target.span_id:
            return i
    raise InterventionTargetNotFoundError(
        f"no event in {target.stream} with span_id {target.span_id!r}"
    )


def apply_intervention(
    spec: InterventionSpec,
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Return (mutated model_calls, mutated tool_calls, matched index).

    The input lists are never mutated (the source capsule stays read-only).
    """
    model_calls = copy.deepcopy(model_calls)
    tool_calls = copy.deepcopy(tool_calls)
    events = model_calls if spec.target.stream == "model-calls" else tool_calls
    idx = _find_target_index(spec, events)
    record = events[idx]
    if spec.replace_model_response is not None:
        record["response"] = spec.replace_model_response
    elif spec.replace_tool_result is not None:
        record["result"] = spec.replace_tool_result
    else:
        assert spec.mutate_payload is not None
        record.update(copy.deepcopy(spec.mutate_payload))
    record["intervened"] = True
    return model_calls, tool_calls, idx


def _resolve_field(record: dict[str, Any], dotted: str) -> Any:
    current: Any = record
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def run_checks(
    checks: list[CheckFunction],
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> list[CheckOutcome]:
    """Evaluate every check; failures are reported, never swallowed."""
    outcomes: list[CheckOutcome] = []
    for check in checks:
        events = model_calls if check.stream == "model-calls" else tool_calls
        if not 0 <= check.event_index < len(events):
            outcomes.append(
                CheckOutcome(
                    name=check.name,
                    passed=False,
                    detail=(
                        f"event_index {check.event_index} out of range for "
                        f"{check.stream}"
                    ),
                )
            )
            continue
        value = _resolve_field(events[check.event_index], check.field)
        if check.equals is not None:
            passed = value == check.equals
            detail = f"{check.field} == {check.equals!r}: got {value!r}"
        else:
            passed = check.contains is not None and isinstance(value, str) and (
                check.contains in value
            )
            detail = f"{check.contains!r} in {check.field}: got {value!r}"
        outcomes.append(CheckOutcome(name=check.name, passed=passed, detail=detail))
    return outcomes


def write_intervention_capsule(
    result_dir: Path,
    replay_id: str,
    source_run_id: str,
    source_manifest: dict[str, Any],
    spec: InterventionSpec,
    matched_index: int,
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    check_outcomes: list[CheckOutcome],
) -> None:
    """Write the output as a minimal, diffable Run Capsule.

    Hard-marked: ``replay_mode: intervention`` + an ``intervention`` block so
    the output can never be mistaken for an original run.
    """
    import json

    result_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "run_id": replay_id,
        "replay_mode": "intervention",
        "replay_of_run_id": source_run_id,
        "command": source_manifest.get("command", []),
        "intervention": {
            **spec.describe(),
            "matched_event_index": matched_index,
            "checks": [o.model_dump() for o in check_outcomes],
        },
    }
    (result_dir / "capsule.yaml").write_text(
        yaml.dump(manifest, allow_unicode=True)
    )
    (result_dir / "model-calls.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in model_calls)
    )
    (result_dir / "tool-calls.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in tool_calls)
    )
