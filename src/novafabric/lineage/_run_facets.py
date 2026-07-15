# src/novafabric/lineage/_run_facets.py
"""NovaFabric custom OpenLineage run facets (NF-036, ADR-0096).

OpenLineage supports **custom facets** — namespaced JSON objects attached to run
events that any OL backend (Marquez, Dataplex, Atlan) carries through untouched. This
module builds the three NovaFabric custom facets plus two standard-shaped run facets
from an already-captured capsule:

- ``novafabric_capsule`` — capsule id, run id, capsule hash (R3)
- ``novafabric_eval``    — eval verdict (``passed``/``failed``/``n/a``) + suite + metrics (R4)
- ``novafabric_policy``  — promotion gate id + policy decision (R5)
- ``executionParameters`` — the standard OL ``ExecutionParametersRunFacet``, populated
  with reproducibility run params (R6)
- ``novafabric_otel_correlation`` — ``trace_id``/``span_id`` for OL↔OTel linking (NF-037, R9/R10)

Invariants: every facet declares a resolvable ``_producer`` and ``_schemaURL`` (R2);
emission is additive (a consumer that ignores custom facets sees unchanged core OL
events, R7); and every facet is validated against a vendored schema *before* it is
attached — a schema violation raises :class:`FacetValidationError` rather than emitting
an invalid event (R11). Facets are only built from data already in the capsule; nothing
is invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

_FACET_PRODUCER = "https://novafabric.io"
_SCHEMA_BASE = "https://novafabric.dev/schemas/ol"

CAPSULE_FACET_KEY = "novafabric_capsule"
EVAL_FACET_KEY = "novafabric_eval"
POLICY_FACET_KEY = "novafabric_policy"
EXEC_PARAMS_FACET_KEY = "executionParameters"
OTEL_CORRELATION_FACET_KEY = "novafabric_otel_correlation"

_EXEC_PARAMS_SCHEMA_URL = (
    "https://openlineage.io/spec/facets/1-0-0/ExecutionParametersRunFacet.json"
)

# Reproducibility run params lifted from capsule.yaml for the ExecutionParameters facet.
_EXEC_PARAM_KEYS = (
    "model",
    "model_version",
    "provider",
    "seed",
    "temperature",
    "top_p",
    "max_tokens",
)


class FacetValidationError(ValueError):
    """A built facet failed validation against its vendored schema (NF-036 R11)."""


def _base(schema_name: str) -> dict[str, Any]:
    return {
        "_producer": _FACET_PRODUCER,
        "_schemaURL": f"{_SCHEMA_BASE}/{schema_name}.json",
    }


# Vendored facet schemas (embedded to avoid package-data concerns). Each requires the
# two OL custom-facet keys plus the facet's own mandatory fields.
_FACET_SCHEMAS: dict[str, dict[str, Any]] = {
    CAPSULE_FACET_KEY: {
        "type": "object",
        "required": ["_producer", "_schemaURL", "capsule_id", "run_id", "capsule_hash"],
        "properties": {
            "capsule_id": {"type": "string", "minLength": 1},
            "run_id": {"type": "string", "minLength": 1},
            "capsule_hash": {"type": "string", "minLength": 1},
        },
    },
    EVAL_FACET_KEY: {
        "type": "object",
        "required": ["_producer", "_schemaURL", "verdict"],
        "properties": {
            "verdict": {"enum": ["passed", "failed", "n/a"]},
            "suite_id": {"type": "string"},
            "metrics": {"type": "array"},
        },
    },
    POLICY_FACET_KEY: {
        "type": "object",
        "required": ["_producer", "_schemaURL", "gate_id", "decision"],
        "properties": {
            "gate_id": {"type": "string"},
            "decision": {"enum": ["allow", "deny", "n/a"]},
        },
    },
    OTEL_CORRELATION_FACET_KEY: {
        "type": "object",
        "required": ["_producer", "_schemaURL", "trace_id", "span_id"],
        "properties": {
            "trace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "span_id": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
        },
    },
}


def _validate(key: str, facet: dict[str, Any]) -> dict[str, Any]:
    schema = _FACET_SCHEMAS.get(key)
    if schema is not None:
        try:
            jsonschema.Draft202012Validator(schema).validate(facet)
        except jsonschema.ValidationError as exc:  # pragma: no cover - message passthrough
            raise FacetValidationError(f"{key} facet invalid: {exc.message}") from exc
    return facet


def capsule_facet(capsule_id: str, run_id: str, capsule_hash: str) -> dict[str, Any]:
    """``novafabric_capsule`` facet (R3)."""
    facet = {
        **_base("novafabric_capsule"),
        "capsule_id": capsule_id,
        "run_id": run_id,
        "capsule_hash": capsule_hash,
    }
    return _validate(CAPSULE_FACET_KEY, facet)


def eval_facet(
    verdict: str, suite_id: str | None = None, metrics: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """``novafabric_eval`` facet (R4). ``verdict`` ∈ {passed, failed, n/a}."""
    facet: dict[str, Any] = {**_base("novafabric_eval"), "verdict": verdict}
    if suite_id:
        facet["suite_id"] = suite_id
    if metrics:
        facet["metrics"] = metrics
    return _validate(EVAL_FACET_KEY, facet)


def policy_facet(gate_id: str, decision: str) -> dict[str, Any]:
    """``novafabric_policy`` facet (R5). ``decision`` ∈ {allow, deny, n/a}."""
    facet = {**_base("novafabric_policy"), "gate_id": gate_id, "decision": decision}
    return _validate(POLICY_FACET_KEY, facet)


def execution_parameters_facet(params: dict[str, Any]) -> dict[str, Any]:
    """Standard OL ``ExecutionParametersRunFacet`` with reproducibility params (R6)."""
    return {
        "_producer": _FACET_PRODUCER,
        "_schemaURL": _EXEC_PARAMS_SCHEMA_URL,
        "parameters": [{"name": k, "value": str(v)} for k, v in params.items()],
    }


def otel_correlation_facet(trace_id: str, span_id: str) -> dict[str, Any]:
    """``novafabric_otel_correlation`` facet (NF-037, R9/R10)."""
    facet = {**_base("novafabric_otel_correlation"), "trace_id": trace_id, "span_id": span_id}
    return _validate(OTEL_CORRELATION_FACET_KEY, facet)


# ---------------------------------------------------------------------------
# Capsule → facet extraction (grounded; nothing invented)
# ---------------------------------------------------------------------------


def _read_eval_verdict(capsule_dir: Path) -> tuple[str, str | None, list[dict[str, Any]] | None]:
    eval_path = capsule_dir / "eval_result.json"
    if not eval_path.exists():
        return "n/a", None, None
    try:
        data = json.loads(eval_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return "n/a", None, None
    passed = data.get("passed")
    if passed is True:
        verdict = "passed"
    elif passed is False:
        verdict = "failed"
    else:
        verdict = "n/a"
    metrics = data.get("metrics") if isinstance(data.get("metrics"), list) else None
    return verdict, data.get("suite_id"), metrics


def build_run_facets(
    capsule_dir: Path,
    manifest: dict[str, Any],
    *,
    with_otel_correlation: bool = False,
) -> dict[str, Any]:
    """Build the NovaFabric custom run facets for a capsule (R1/R3/R4/R5/R6/R9).

    Reads only capsule-resident data. Facets whose source data is absent are still
    emitted with honest defaults (``verdict: n/a``, ``decision: n/a``) so the facet
    set is stable. Raises :class:`FacetValidationError` if any facet is malformed.
    """
    from novafabric.evidence.merkle import capsule_merkle_root

    run_id = str(manifest.get("run_id", capsule_dir.name))
    capsule_hash = capsule_merkle_root(capsule_dir)

    facets: dict[str, Any] = {
        CAPSULE_FACET_KEY: capsule_facet(capsule_dir.name, run_id, capsule_hash),
    }

    verdict, suite_id, metrics = _read_eval_verdict(capsule_dir)
    facets[EVAL_FACET_KEY] = eval_facet(verdict, suite_id, metrics)

    gate_id = str(manifest.get("promotion_gate") or manifest.get("policy_gate") or "n/a")
    decision = str(manifest.get("policy_decision") or "n/a")
    if decision not in {"allow", "deny", "n/a"}:
        decision = "n/a"
    facets[POLICY_FACET_KEY] = policy_facet(gate_id, decision)

    exec_params = {k: manifest[k] for k in _EXEC_PARAM_KEYS if manifest.get(k) is not None}
    if exec_params:
        facets[EXEC_PARAMS_FACET_KEY] = execution_parameters_facet(exec_params)

    if with_otel_correlation:
        trace_id = str(manifest.get("trace_id", "")).lower()
        span_id = str(manifest.get("span_id", "")).lower()
        if len(trace_id) == 32 and len(span_id) == 16:
            facets[OTEL_CORRELATION_FACET_KEY] = otel_correlation_facet(trace_id, span_id)

    return facets
