"""Capsule-directory scanner for the offline metrics query index (ADR-0129 P2).

Extracts flat, typed rows from local Run Capsules for the derived query index.
Reuses existing extraction paths (D4 — single definition of each metric):

- **cost** — the recorded ``nova.cost`` extension on model-call records,
  written at capture time by ``cost/`` (ADR-0066); never recomputed here.
- **tokens / latency** — the capsule's ``model-calls.jsonl`` records
  (OTel GenAI semconv fields).
- **scores** — ``scores.jsonl`` via :mod:`novafabric.eval.scores`.

Row semantics (documented, deterministic):

- One :class:`CallRow` per model call, with capsule-level dimensions
  denormalized onto it. A capsule with **zero** model calls contributes one
  synthetic row (``model``/metrics ``None``) so it still counts in
  ``count()`` and status/dimension filters.
- One :class:`ScoreRow` per numeric or boolean score (boolean → 1.0/0.0;
  categorical scores are not aggregatable and are skipped). Score rows carry
  the capsule dimensions but no ``model`` (scores attach to the capsule).
- Dimensions read additively: ``deployment_environment`` (ADR-0126) and
  ``variant`` (ADR-0116 — the block's ``variant_id``) from their top-level
  capsule fields when present, falling back to same-named ``metadata``
  labels; ``asset`` / ``tag`` from ``metadata``; ``log_level`` (ADR-0127)
  from the model-call record, absent ⇒ ``info`` per the spec's
  default-reading rule. Absent dimensions are
  ``None`` and never match a filter predicate.

Reading is strictly read-only: this module never writes to the capsule
directory (ADR-0129 D3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from novafabric.eval.scores import SCORES_FILENAME, ScoreValueType, read_scores
from novafabric.query.errors import QueryIndexError
from novafabric.query.model import LOG_LEVEL_ALIASES, LOG_LEVEL_RANKS

_MANIFEST_NAMES = ("capsule.yaml", "capsule.json")

MODEL_CALLS_FILENAME = "model-calls.jsonl"

#: Every capsule file :func:`scan_capsule` opens — the single source of truth for
#: "what this indexer reads". The ADR-0225 row cache derives its change signature
#: from this tuple, so a file added here is covered by the signature and a file
#: read *without* being added here is a stale-answer defect (an append moves
#: neither the directory mtime nor any signed-for file). Extend this whenever the
#: indexer learns to read something new.
INDEXED_FILENAMES: tuple[str, ...] = (
    *_MANIFEST_NAMES,
    MODEL_CALLS_FILENAME,
    SCORES_FILENAME,
)


@dataclass(frozen=True)
class CallRow:
    """One model call (or one synthetic zero-call row) with capsule dims."""

    run_id: str
    created_at: float  # epoch seconds, UTC
    status: str | None
    asset: str | None
    deployment_environment: str | None
    variant: str | None
    log_level: str | None
    tag: str | None
    model: str | None
    model_id: str | None
    cost: float | None
    prompt_tokens: float | None
    completion_tokens: float | None
    total_tokens: float | None
    latency: float | None
    #: Currency of the recorded ``nova.cost`` block (additive, ADR-0131);
    #: ``None`` when no cost was recorded or the record carries no currency.
    cost_currency: str | None = None


@dataclass(frozen=True)
class ScoreRow:
    """One aggregatable eval score with its capsule's dimensions."""

    run_id: str
    created_at: float
    status: str | None
    asset: str | None
    deployment_environment: str | None
    variant: str | None
    log_level: str | None
    tag: str | None
    model: str | None
    model_id: str | None
    name: str
    value: float


@dataclass(frozen=True)
class IndexRows:
    """Everything the engine needs to build the derived in-memory index."""

    calls: list[CallRow]
    scores: list[ScoreRow]
    capsule_count: int


def _to_epoch(value: Any, *, source: Path) -> float:
    if not isinstance(value, str) or not value:
        raise QueryIndexError(f"{source}: capsule manifest has no parsable created_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueryIndexError(f"{source}: invalid created_at {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _opt_num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _load_manifest(capsule_dir: Path) -> dict[str, Any] | None:
    for name in _MANIFEST_NAMES:
        path = capsule_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if name.endswith(".json") else yaml.safe_load(text)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise QueryIndexError(f"{path}: unreadable capsule manifest: {exc}") from exc
        if not isinstance(data, dict):
            raise QueryIndexError(f"{path}: capsule manifest is not a mapping")
        return data
    return None


def _variant_of(manifest: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    raw = manifest.get("variant", metadata.get("variant"))
    if isinstance(raw, dict):
        # ADR-0116 canonical block: the queryable dimension is variant_id
        # (the read conveniences select by recorded variant_id per the ADR's
        # CLI/API surface). Legacy ad-hoc keys are kept as fallbacks.
        for key in ("variant_id", "variant", "name", "id"):
            found = _opt_str(raw.get(key))
            if found is not None:
                return found
        return None
    return _opt_str(raw)


def _log_level_of(record: dict[str, Any]) -> str:
    raw = record.get("log_level")
    if isinstance(raw, str):
        level = LOG_LEVEL_ALIASES.get(raw.lower(), raw.lower())
        if level in LOG_LEVEL_RANKS:
            return level
    return "info"  # ADR-0127: readers treat an absent log_level as info


def _model_call_rows(capsule_dir: Path, dims: dict[str, Any]) -> list[CallRow]:
    path = capsule_dir / MODEL_CALLS_FILENAME
    rows: list[CallRow] = []
    if path.is_file():
        for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise QueryIndexError(f"{path}:{line_no}: invalid model-call record") from exc
            if not isinstance(record, dict):
                raise QueryIndexError(f"{path}:{line_no}: model-call record is not an object")
            model = _opt_str(record.get("gen_ai.response.model")) or _opt_str(
                record.get("gen_ai.request.model")
            )
            prompt = _opt_num(record.get("gen_ai.usage.input_tokens"))
            completion = _opt_num(record.get("gen_ai.usage.output_tokens"))
            total: float | None
            if prompt is None and completion is None:
                total = None
            else:
                total = (prompt or 0.0) + (completion or 0.0)
            cost_block = record.get("nova.cost")
            cost = _opt_num(cost_block.get("amount")) if isinstance(cost_block, dict) else None
            currency = (
                _opt_str(cost_block.get("currency")) if isinstance(cost_block, dict) else None
            )
            rows.append(
                CallRow(
                    **dims,
                    log_level=_log_level_of(record),
                    model=model,
                    model_id=model,
                    cost=cost,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=total,
                    latency=_opt_num(record.get("duration_ms")),
                    cost_currency=currency if cost is not None else None,
                )
            )
    if not rows:
        # Synthetic zero-call row: the capsule still exists for count()/filters.
        rows.append(
            CallRow(
                **dims,
                log_level="info",
                model=None,
                model_id=None,
                cost=None,
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                latency=None,
            )
        )
    return rows


def _score_rows(capsule_dir: Path, dims: dict[str, Any]) -> list[ScoreRow]:
    path = capsule_dir / SCORES_FILENAME
    if not path.is_file():
        return []
    try:
        scores = read_scores(path)
    except ValueError as exc:
        raise QueryIndexError(f"{path}: invalid scores.jsonl: {exc}") from exc
    rows: list[ScoreRow] = []
    for score in scores:
        if score.value_type is ScoreValueType.NUMERIC and isinstance(score.value, (int, float)):
            value = float(score.value)
        elif score.value_type is ScoreValueType.BOOLEAN:
            value = 1.0 if score.value else 0.0
        else:  # categorical — not aggregatable
            continue
        rows.append(
            ScoreRow(**dims, log_level=None, model=None, model_id=None,
                     name=score.name, value=value)
        )
    return rows


def scan_capsule(capsule_dir: Path) -> tuple[list[CallRow], list[ScoreRow]] | None:
    """Scan one capsule directory into index rows.

    Returns ``None`` when the directory carries no capsule manifest (it is
    not a capsule); raises :class:`QueryIndexError` when the capsule is
    present but unreadable/malformed. Extraction is the single definition
    shared by ``nova query`` (ADR-0129) and ``nova trend`` (ADR-0131).
    """
    manifest = _load_manifest(capsule_dir)
    if manifest is None:
        return None
    metadata_raw = manifest.get("metadata")
    metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
    run_id = _opt_str(manifest.get("run_id")) or capsule_dir.name
    dims: dict[str, Any] = {
        "run_id": run_id,
        "created_at": _to_epoch(manifest.get("created_at"), source=capsule_dir),
        "status": _opt_str(manifest.get("status")),
        "asset": _opt_str(manifest.get("asset", metadata.get("asset"))),
        "deployment_environment": _opt_str(
            manifest.get("deployment_environment", metadata.get("deployment_environment"))
        ),
        "variant": _variant_of(manifest, metadata),
        "tag": _opt_str(metadata.get("tag")),
    }
    return _model_call_rows(capsule_dir, dims), _score_rows(capsule_dir, dims)


def scan_capsule_dir(capsule_dir: str | Path) -> IndexRows:
    """Scan a local capsule directory into index rows.

    Every immediate subdirectory containing a ``capsule.yaml`` /
    ``capsule.json`` manifest is one capsule; anything else is ignored. An
    unreadable manifest or record fails with a clear :class:`QueryIndexError`
    — never a silent wrong answer (ADR-0129 D3). An empty or capsule-free
    directory yields zero rows (not an error).
    """
    base = Path(capsule_dir)
    if not base.is_dir():
        raise QueryIndexError(f"capsule directory not found: {base}")
    calls: list[CallRow] = []
    score_rows: list[ScoreRow] = []
    capsule_count = 0
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        scanned = scan_capsule(child)
        if scanned is None:
            continue
        capsule_count += 1
        capsule_calls, capsule_scores = scanned
        calls.extend(capsule_calls)
        score_rows.extend(capsule_scores)
    return IndexRows(calls=calls, scores=score_rows, capsule_count=capsule_count)
