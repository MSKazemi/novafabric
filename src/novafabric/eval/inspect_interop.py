# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Inspect-AI eval-log interop — score-level bridge (NF-024, ADR-0108).

Imports the **scorer results** of an Inspect AI (UK AISI) JSON eval log as
evidence-grade :class:`~novafabric.eval.scores.Score` records, and exports a
capsule's ``scores.jsonl`` back as an Inspect-compatible JSON log. Parsing is
**pure stdlib JSON against the documented Inspect log structure** — interop
without installing ``inspect-ai`` (spec: ``design/spec/features/
NF-024-028-inspect-interop.md``).

Honesty invariants (ADR-0021 §4, spec R2–R4):

- **Versioned mapping.** The bridge supports the pinned Inspect JSON log
  ``version`` values in :data:`SUPPORTED_LOG_VERSIONS`; anything else errors
  naming the unsupported version. Every import is stamped with
  :data:`INSPECT_MAPPING_VERSION`.
- **Nothing dropped silently.** Inspect fields with no ``Score`` target are
  preserved in the result's ``unmapped`` block (``org.inspect.*`` namespace on
  disk). Content-bearing fields (prompts, model output, message transcripts)
  are **enumerated by name** in ``omitted`` but deliberately not copied —
  full-content span import is the planned capsule-level bridge, and NovaFabric
  never captures prompt/response bytes by default.
- **Foreign provenance is explicit.** Imported scores carry
  ``evaluator_id="inspect-ai:<scorer>"`` and a *synthetic* content-addressed
  ``eval_card_digest`` derived from the foreign scorer identity — it
  identifies the Inspect scorer + mapping version; it is **not** a signed
  NovaFabric eval card (NF-002).

Scope note (docs honesty): this is the **score-level** slice of NF-024. The
Solver-steps → span-tree import and byte-equal native round-trip from the spec
remain planned.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    read_scores,
)

#: Version of the Inspect ↔ NovaFabric score mapping implemented here (spec R2).
INSPECT_MAPPING_VERSION = "1.0.0"

#: Pinned Inspect JSON eval-log ``version`` values this bridge understands.
SUPPORTED_LOG_VERSIONS: tuple[int, ...] = (2,)

#: Reverse-DNS extension namespace for preserved Inspect-only data (spec R3).
INSPECT_EXTENSION_NS = "org.inspect"

#: Capsule-relative path of the import record written by ``nova eval import-inspect``.
IMPORT_RECORD_PATH = Path("extensions") / INSPECT_EXTENSION_NS / "import.json"

#: Sample fields that may carry prompt/response content — enumerated, never copied
#: (ADR-0021 §4; the span-level bridge is the planned home for content import).
_CONTENT_FIELDS = (
    "input", "choices", "target", "messages", "output", "events", "store", "attachments",
)

#: Sample fields the score mapping consumes directly.
_MAPPED_SAMPLE_FIELDS = ("id", "epoch", "scores")

#: ``eval`` header fields consumed into :class:`InspectProvenance`.
_MAPPED_EVAL_FIELDS = ("run_id", "created", "task", "task_id", "model", "dataset")


class InspectLogError(ValueError):
    """The file is not a parseable/supported Inspect JSON eval log."""


class InspectProvenance(BaseModel):
    """Where an imported batch of scores came from (spec R2/R4)."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["inspect-ai"] = "inspect-ai"
    mapping_version: str = INSPECT_MAPPING_VERSION
    log_version: int
    task: str = ""
    task_id: str = ""
    inspect_run_id: str = ""
    model: str = ""
    dataset_name: str = ""
    created: str = ""


class InspectImportResult(BaseModel):
    """Everything an Inspect log import produced — scores plus honesty ledger."""

    model_config = ConfigDict(extra="forbid")

    provenance: InspectProvenance
    scores: list[Score] = Field(default_factory=list)
    #: Inspect fields with no Score target, preserved verbatim (spec R3).
    unmapped: dict[str, Any] = Field(default_factory=dict)
    #: Content-bearing fields enumerated by name but not copied (ADR-0021 §4).
    omitted: list[str] = Field(default_factory=list)


def _digest(obj: Any) -> str:
    """``sha256:<hex>`` over the canonical JSON of *obj* (content-addressed)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scorer_card_digest(scorer: str) -> str:
    """Synthetic content-addressed identity for a foreign Inspect scorer."""
    return _digest(
        {"source": "inspect-ai", "scorer": scorer, "mapping_version": INSPECT_MAPPING_VERSION}
    )


def _scorer_source(scorer: str) -> ScoreSource:
    """Inspect scorer kind → ``Score.source`` (spec R1: ``judge|code``).

    Inspect's model-graded scorers follow the documented ``model_graded_*``
    naming convention; everything else is deterministic scorer code.
    """
    return ScoreSource.JUDGE if scorer.startswith("model_graded") else ScoreSource.CODE


def _map_value(value: Any) -> tuple[bool | float | str, ScoreValueType] | None:
    """Map an Inspect score value to ``(value, value_type)``; ``None`` = unmappable.

    Inspect's ``"C"``/``"I"``/``"P"``/``"N"`` verdict strings stay categorical —
    no lossy coercion. Structured (dict/list) values have no Score target and go
    to the ``unmapped`` block instead.
    """
    if isinstance(value, bool):
        return value, ScoreValueType.BOOLEAN
    if isinstance(value, (int, float)):
        return float(value), ScoreValueType.NUMERIC
    if isinstance(value, str):
        return value, ScoreValueType.CATEGORICAL
    return None


def import_inspect_log(path: str | Path) -> InspectImportResult:
    """Import an Inspect AI JSON eval log's scores as NovaFabric ``Score`` records.

    Raises :class:`FileNotFoundError` when *path* does not exist and
    :class:`InspectLogError` when it is not a parseable Inspect JSON log at a
    supported (pinned) log version.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Inspect log not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InspectLogError(f"{p}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InspectLogError(f"{p}: expected a JSON object at the top level")

    version = data.get("version")
    if not isinstance(version, int):
        raise InspectLogError(f"{p}: missing integer 'version' — not an Inspect eval log")
    if version not in SUPPORTED_LOG_VERSIONS:
        raise InspectLogError(
            f"{p}: unsupported Inspect log version {version} "
            f"(supported: {', '.join(str(v) for v in SUPPORTED_LOG_VERSIONS)})"
        )

    eval_block = data.get("eval")
    if not isinstance(eval_block, dict):
        raise InspectLogError(f"{p}: missing 'eval' header block — not an Inspect eval log")
    samples = data.get("samples", [])
    if not isinstance(samples, list):
        raise InspectLogError(f"{p}: 'samples' must be a list")

    dataset = eval_block.get("dataset")
    dataset_name = dataset.get("name", "") if isinstance(dataset, dict) else ""
    provenance = InspectProvenance(
        log_version=version,
        task=str(eval_block.get("task", "") or ""),
        task_id=str(eval_block.get("task_id", "") or ""),
        inspect_run_id=str(eval_block.get("run_id", "") or ""),
        model=str(eval_block.get("model", "") or ""),
        dataset_name=str(dataset_name or ""),
        created=str(eval_block.get("created", "") or ""),
    )

    unmapped: dict[str, Any] = {}
    omitted: list[str] = []
    scores: list[Score] = []

    # eval-header fields with no native target: preserved (spec R3).
    for key, value in eval_block.items():
        if key not in _MAPPED_EVAL_FIELDS:
            unmapped[f"eval.{key}"] = value

    # Per-sample scorer results → span-subject Scores.
    for idx, sample in enumerate(samples):
        if not isinstance(sample, dict):
            unmapped[f"samples[{idx}]"] = sample
            continue
        sample_id = sample.get("id", idx)
        epoch = sample.get("epoch", 1)
        subject = _digest({"task": provenance.task, "sample_id": sample_id, "epoch": epoch})
        sample_scores = sample.get("scores")
        if isinstance(sample_scores, dict):
            for scorer, record in sample_scores.items():
                loc = f"samples[{idx}].scores.{scorer}"
                if not scorer or not isinstance(record, dict):
                    unmapped[loc] = record
                    continue
                mapped = _map_value(record.get("value"))
                if mapped is None:
                    # A structured/absent value has no Score target — preserved,
                    # never silently dropped.
                    unmapped[f"{loc}.value"] = record.get("value")
                else:
                    value, value_type = mapped
                    scores.append(
                        Score(
                            subject=subject,
                            subject_kind="span",
                            name=scorer,
                            value=value,
                            value_type=value_type,
                            source=_scorer_source(scorer),
                            evaluator_id=f"inspect-ai:{scorer}",
                            eval_card_digest=_scorer_card_digest(scorer),
                        )
                    )
                extras = {
                    k: v
                    for k, v in record.items()
                    if k != "value" and v is not None
                }
                if extras:
                    unmapped[loc] = extras
        # Content-bearing fields: enumerated by name, not copied (ADR-0021 §4).
        for field in _CONTENT_FIELDS:
            if field in sample:
                omitted.append(f"samples[{idx}].{field}")
        for key, value in sample.items():
            if key not in _MAPPED_SAMPLE_FIELDS and key not in _CONTENT_FIELDS:
                unmapped[f"samples[{idx}].{key}"] = value

    # Aggregate results → capsule-subject Scores (one per scorer metric).
    results = data.get("results")
    if isinstance(results, dict):
        agg_subject = _digest(
            {"task": provenance.task, "task_id": provenance.task_id, "scope": "results"}
        )
        for entry_idx, entry in enumerate(results.get("scores", []) or []):
            if not isinstance(entry, dict):
                unmapped[f"results.scores[{entry_idx}]"] = entry
                continue
            scorer = str(entry.get("scorer") or entry.get("name") or "")
            if not scorer:
                unmapped[f"results.scores[{entry_idx}]"] = entry
                continue
            metrics = entry.get("metrics")
            if not isinstance(metrics, dict):
                continue
            for metric_name, metric in metrics.items():
                metric_value = metric.get("value") if isinstance(metric, dict) else metric
                mapped = _map_value(metric_value)
                if mapped is None or mapped[1] is not ScoreValueType.NUMERIC:
                    unmapped[f"results.scores.{scorer}.metrics.{metric_name}"] = metric
                    continue
                scores.append(
                    Score(
                        subject=agg_subject,
                        subject_kind="capsule",
                        name=f"{scorer}/{metric_name}",
                        value=mapped[0],
                        value_type=ScoreValueType.NUMERIC,
                        source=_scorer_source(scorer),
                        evaluator_id=f"inspect-ai:{scorer}",
                        eval_card_digest=_scorer_card_digest(scorer),
                    )
                )

    return InspectImportResult(
        provenance=provenance, scores=scores, unmapped=unmapped, omitted=omitted
    )


def _read_import_record(capsule_dir: Path) -> dict[str, Any]:
    """The ``org.inspect`` import record, if this capsule was imported (else {})."""
    record_path = capsule_dir / IMPORT_RECORD_PATH
    if not record_path.is_file():
        return {}
    try:
        loaded = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def export_inspect_log(capsule: str | Path) -> dict[str, Any]:
    """Export a capsule's ``scores.jsonl`` as an Inspect-compatible JSON log dict.

    A capsule without a score log exports a valid, honest, empty log (additive-
    first). When the capsule was itself imported from Inspect, the preserved
    ``org.inspect`` header (task/model/run id) is restored so the Inspect-native
    header portion round-trips.
    """
    capsule_dir = Path(capsule)
    if not capsule_dir.is_dir():
        raise FileNotFoundError(f"not a capsule directory: {capsule_dir}")
    scores = read_scores(capsule_dir / SCORES_FILENAME)

    record = _read_import_record(capsule_dir)
    prov = record.get("provenance", {}) if isinstance(record.get("provenance"), dict) else {}

    span_scores = [s for s in scores if s.subject_kind == "span"]
    capsule_scores = [s for s in scores if s.subject_kind == "capsule"]

    # One Inspect sample per distinct span subject, in first-seen order.
    subjects: list[str] = []
    for s in span_scores:
        if s.subject not in subjects:
            subjects.append(s.subject)

    samples: list[dict[str, Any]] = []
    for i, subject in enumerate(subjects, start=1):
        sample_scores: dict[str, Any] = {}
        for s in span_scores:
            if s.subject != subject:
                continue
            sample_scores[s.name] = {
                "value": s.value,
                "answer": None,
                "explanation": None,
                "metadata": {
                    "dev.novafabric.score_id": s.score_id,
                    "dev.novafabric.subject": s.subject,
                    "dev.novafabric.source": s.source.value,
                    "dev.novafabric.eval_card_digest": s.eval_card_digest,
                },
            }
        samples.append({"id": i, "epoch": 1, "scores": sample_scores})

    # Aggregate metrics: booleans → accuracy (pass rate), numerics → mean.
    results_scores: list[dict[str, Any]] = []
    names: list[str] = []
    for s in span_scores:
        if s.name not in names:
            names.append(s.name)
    for name in names:
        group = [s for s in span_scores if s.name == name]
        metrics: dict[str, Any] = {}
        booleans = [s for s in group if s.value_type is ScoreValueType.BOOLEAN]
        numerics = [s for s in group if s.value_type is ScoreValueType.NUMERIC]
        if booleans:
            rate = sum(1 for s in booleans if s.value) / len(booleans)
            metrics["accuracy"] = {"name": "accuracy", "value": rate}
        if numerics:
            mean = sum(float(s.value) for s in numerics) / len(numerics)
            metrics["mean"] = {"name": "mean", "value": mean}
        metrics["count"] = {"name": "count", "value": len(group)}
        results_scores.append(
            {"name": name, "scorer": name, "metrics": metrics, "params": {}}
        )
    # Capsule-level scores (e.g. re-exported Inspect aggregates) pass through.
    for s in capsule_scores:
        scorer, _, metric = s.name.partition("/")
        results_scores.append(
            {
                "name": s.name,
                "scorer": scorer or s.name,
                "metrics": {
                    (metric or "value"): {"name": metric or "value", "value": s.value}
                },
                "params": {},
            }
        )

    return {
        "version": prov.get("log_version", SUPPORTED_LOG_VERSIONS[-1]),
        "status": "success",
        "eval": {
            "run_id": prov.get("inspect_run_id", ""),
            "created": prov.get("created", ""),
            "task": prov.get("task", "novafabric/capsule"),
            "task_id": prov.get("task_id", ""),
            "model": prov.get("model", "novafabric/unknown"),
            "dataset": {"name": prov.get("dataset_name", ""), "samples": len(samples)},
            "packages": {},
            "metadata": {
                "dev.novafabric.exporter": "novafabric",
                "dev.novafabric.mapping_version": INSPECT_MAPPING_VERSION,
            },
        },
        "plan": {"name": "plan", "steps": []},
        "results": {
            "total_samples": len(samples),
            "completed_samples": len(samples),
            "scores": results_scores,
        },
        "stats": {"started_at": "", "completed_at": ""},
        "samples": samples,
    }
