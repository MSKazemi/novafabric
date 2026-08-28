"""``CapsuleView`` summary projection (ADR-0140 P1).

Builds the bounded, non-sensitive summary document embedded in the shareable
single-file HTML viewer. The projection copies **only** fields the capsule
already exposes (``capsule.yaml``, ``model-calls.jsonl``, ``tool-calls.jsonl``,
``scores.jsonl``, ``lineage.jsonl``) — it never adds content the capsule does
not carry, never reads tool arguments/results or message bodies, and preserves
redaction markers verbatim (ADR-0140 D2/D3; ADR-0009). There is no un-redact
path and no ``--show-secrets`` flag.

Wire contract: ``schemas/capsule-view.schema.json`` (``schema_version 0.1.0``);
spec: ``the private design/spec/capsule-viewer-v0.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

import yaml

#: ``CapsuleView.schema_version`` — tracks ``schemas/capsule-view.schema.json``.
CAPSULE_VIEW_SCHEMA_VERSION = "0.1.0"

_VERIFY_NOTE = (
    "Summary view — not the signed Evidence Bundle. "
    "Run `nova verify <capsule-dir>` for real verification."
)


@dataclass
class CapsuleViewResult:
    """A built ``CapsuleView`` plus any per-section skip warnings.

    ``warnings`` lists sections that were unreadable and therefore skipped
    (the export is read-only and non-blocking per ADR-0140 D6 — a partially
    readable capsule still produces a file).
    """

    view: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def _generator_version() -> str:
    try:
        return _dist_version("novafabric")
    except PackageNotFoundError:  # pragma: no cover - packaging edge
        return "0.0.0"


def _int_or_none(value: Any) -> int | None:
    """Coerce a capsule-recorded numeric field to ``int | None`` (bools are not counts)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file; ``[]`` if absent; ``ValueError`` on an unparseable line."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{line_no}: invalid JSON: {exc}") from exc
        if isinstance(record, dict):
            records.append(record)
    return records


def _model_call_row(record: dict[str, Any]) -> dict[str, Any]:
    model = record.get("gen_ai.response.model") or record.get("gen_ai.request.model")
    return {
        "model_call_id": str(record.get("model_call_id", "")),
        "model": str(model) if model else "unknown",
        "status": str(record.get("status", "unknown")),
        "input_tokens": _int_or_none(record.get("gen_ai.usage.input_tokens")),
        "output_tokens": _int_or_none(record.get("gen_ai.usage.output_tokens")),
        "latency_ms": _int_or_none(record.get("duration_ms")),
    }


def _tool_call_row(record: dict[str, Any]) -> dict[str, Any]:
    duration = record.get("duration_ms")
    if duration is None:
        duration = record.get("latency_ms")
    return {
        "tool_call_id": str(record.get("tool_call_id", "")),
        "tool_name": str(record.get("tool_name", "unknown")),
        "mutation_class": str(record.get("mutation_class", "unknown")),
        "status": str(record.get("status", "unknown")),
        "duration_ms": _int_or_none(duration),
    }


def _score_row(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("value")
    numeric: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
    return {"suite": str(record.get("name", "unknown")), "value": numeric}


def _lineage_refs(records: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: list[str] = []
    edges: list[dict[str, str]] = []
    for record in records:
        source = str(record.get("source", "unknown"))
        target = str(record.get("target", "unknown"))
        for node in (source, target):
            if node not in nodes:
                nodes.append(node)
        edge_type = str(record.get("edge_type", "unknown"))
        edges.append({"from": source, "to": target, "type": edge_type})
    return {"nodes": nodes, "edges": edges}


def _load_manifest(capsule_dir: Path) -> dict[str, Any]:
    if not capsule_dir.exists():
        raise FileNotFoundError(f"capsule directory not found: {capsule_dir}")
    if not capsule_dir.is_dir():
        raise NotADirectoryError(f"not a capsule directory: {capsule_dir}")
    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"capsule.yaml not found in {capsule_dir}")
    loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def build_capsule_view(capsule_dir: str | Path, *, title: str | None = None) -> CapsuleViewResult:
    """Project *capsule_dir* into a ``CapsuleView`` dict (plus skip warnings).

    Raises ``FileNotFoundError`` / ``NotADirectoryError`` when the capsule (or
    its ``capsule.yaml``) is missing. Unreadable *section* files (bad JSONL)
    are skipped with a warning instead — the view is still produced.
    """
    capsule_dir = Path(capsule_dir)
    manifest = _load_manifest(capsule_dir)
    warnings: list[str] = []

    header: dict[str, Any] = {
        "run_id": str(manifest.get("run_id", capsule_dir.name)),
        "capture_mode": str(manifest.get("capture_mode", "unknown")),
        "started_at": str(manifest.get("created_at", "")),
        "finished_at": str(manifest.get("finished_at", "")),
    }
    # Optional header fields: projected only when the capsule carries them (D2).
    metadata = manifest.get("metadata")
    agent_id = manifest.get("agent_id")
    if agent_id is None and isinstance(metadata, dict):
        agent_id = metadata.get("agent_id")
    if agent_id is not None:
        header["agent_id"] = str(agent_id)
    if manifest.get("capsule_hash") is not None:
        header["capsule_hash"] = str(manifest["capsule_hash"])

    def _section(filename: str) -> list[dict[str, Any]]:
        try:
            return _read_jsonl(capsule_dir / filename)
        except (ValueError, OSError) as exc:
            warnings.append(f"{filename} unreadable — section skipped ({exc})")
            return []

    view: dict[str, Any] = {
        "schema_version": CAPSULE_VIEW_SCHEMA_VERSION,
        "generator": {"name": "novafabric", "version": _generator_version()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capsule": header,
        "model_calls": [_model_call_row(r) for r in _section("model-calls.jsonl")],
        "tool_calls": [_tool_call_row(r) for r in _section("tool-calls.jsonl")],
        "scores": [_score_row(r) for r in _section("scores.jsonl")],
        "lineage_refs": _lineage_refs(_section("lineage.jsonl")),
    }
    if title is not None:
        view["title"] = title
    notes = _VERIFY_NOTE
    if warnings:
        notes += " Skipped sections: " + "; ".join(warnings)
    view["notes"] = notes
    return CapsuleViewResult(view=view, warnings=warnings)
