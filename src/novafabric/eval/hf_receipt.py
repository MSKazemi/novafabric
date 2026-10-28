"""Evaluation receipts that bind an experiment to pinned Hugging Face references (HF-05).

This module *consumes* what the eval subsystem already produces -- a finalized, content-hashed
:class:`~novafabric.eval.experiment.Experiment` -- and the Hub revision receipts of
:mod:`novafabric.interop.hf_hub`. It is not a scoring engine, a runner or a leaderboard: every
number in a receipt is copied or derived (counts, Wilson interval) from the experiment, and no
function here can produce a score the experiment does not hold.

What a receipt establishes, and what it does not (the ``limitations`` list says so per receipt):

* the dataset is *named* by repo id, config, split and -- when the caller resolved it --
  an immutable commit; a branch name is reported as a moving ref;
* the Hub revision and the local dataset content hash are **not** cross-checked by this module;
* NovaFabric-recorded evidence is **not** a Hugging Face "verified" score and says nothing about
  contamination of the benchmark.

Hub output is opt-in and local: :func:`write_eval_results` writes a file you name; nothing here
opens a connection or touches a model repo. The ``.eval_results`` format is a Hub work in
progress (https://huggingface.co/docs/hub/eval-results, checked 2026-10-28) and defines no
scale for ``value``, so the scale is stated in ``notes`` and ``verifyToken`` is never emitted.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field

from novafabric.capture.secrets import scan_text_rule_ids
from novafabric.eval.experiment import Experiment, ItemRunStatus, MetricAggregate
from novafabric.eval.experiment_compare import ExperimentComparison
from novafabric.eval.significance import wilson_interval
from novafabric.interop.hf_hub import HubReference

SCHEMA_VERSION = "0.1.0"
CARD_BLOCK_BEGIN = "<!-- novafabric-evidence:begin -->"
CARD_BLOCK_END = "<!-- novafabric-evidence:end -->"
#: Below this many scored items the report says the interval is wide. A reading aid, not a test.
SMALL_SAMPLE = 30
NOT_VERIFIED = (
    "Evidence recorded by NovaFabric; this is not a Hugging Face verified score and does not "
    "show the benchmark is uncontaminated."
)

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_ENTRY_KEYS = {"dataset", "value", "verifyToken", "date", "source", "notes"}
_DATASET_KEYS = {"id", "task_id", "revision"}
_SOURCE_KEYS = {"url", "name", "user", "org"}
_MAX_NOTES = 600


class EvalReceiptError(ValueError):
    """The inputs cannot produce an honest receipt or a valid export."""


class HarnessRef(BaseModel):
    """The harness that ran the evaluation. Both fields are claims by whoever supplies them."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    version: str | None = Field(default=None, max_length=64)
    container_digest: str | None = None


class ItemCounts(BaseModel):
    """How many dataset items did what. ``unscored`` is everything that carries no score."""

    model_config = ConfigDict(extra="forbid")

    attempted: int = Field(ge=0)
    ok: int = Field(ge=0)
    errored: int = Field(ge=0)
    skipped: int = Field(ge=0)
    scored: int = Field(ge=0)
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)
    unscored: int = Field(ge=0)


class MetricReport(BaseModel):
    """One aggregate, restated with its uncertainty. ``value`` is copied, never recomputed."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    reducer: str
    value_type: Literal["boolean", "numeric"]
    value: float
    n: int = Field(ge=0)
    interval: tuple[float, float] | None = None
    interval_method: Literal["wilson_95"] | None = None


class EvalReceipt(BaseModel):
    """A dataset-experiment tied to pinned Hub references, with its limits spelled out."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    experiment_id: str
    experiment_content_hash: str
    dataset: HubReference
    local_dataset: dict[str, str]
    target_ref: str
    model: HubReference | None = None
    model_declared: str | None = None
    harness: HarnessRef | None = None
    scorer_ref: str | None = None
    scorer_config_digest: str | None = None
    template_digest: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    counts: ItemCounts
    metrics: list[MetricReport]
    limitations: list[str]
    claim: str = NOT_VERIFIED


def _plain_text(value: str | None, field: str, limit: int = 200) -> str | None:
    """Caller-supplied free text goes into a receipt that may be published: refuse a credential."""
    if value is None:
        return None
    if len(value) > limit or scan_text_rule_ids(value):
        raise EvalReceiptError(f"{field} is too long or looks like a credential")
    return value


def _digest(value: str | None, field: str) -> str | None:
    if value is not None and not _SHA256_RE.match(value):
        raise EvalReceiptError(f"{field} must be 'sha256:<64 hex>'")
    return value


def _counts(experiment: Experiment, metric: MetricAggregate | None) -> ItemCounts:
    statuses = [run.status for run in experiment.runs]
    attempted = len(statuses)
    scored = metric.n if metric else 0
    passed = failed = None
    if metric is not None and metric.value_type == "boolean":
        passed = round(metric.value * metric.n)
        failed = metric.n - passed
    return ItemCounts(
        attempted=attempted,
        ok=statuses.count(ItemRunStatus.OK),
        errored=statuses.count(ItemRunStatus.ERROR),
        skipped=statuses.count(ItemRunStatus.SKIPPED),
        scored=scored,
        passed=passed,
        failed=failed,
        unscored=attempted - scored,
    )


def _report(aggregate: MetricAggregate) -> MetricReport:
    interval = aggregate.wilson
    method: Literal["wilson_95"] | None = "wilson_95" if interval is not None else None
    if interval is None and aggregate.value_type == "boolean":
        interval = wilson_interval(round(aggregate.value * aggregate.n), aggregate.n)
        method = "wilson_95"
    return MetricReport(
        metric=aggregate.metric,
        reducer=aggregate.reducer,
        value_type=aggregate.value_type,
        value=aggregate.value,
        n=aggregate.n,
        interval=interval,
        interval_method=method,
    )


def _limitations(
    dataset: HubReference,
    model: HubReference | None,
    model_declared: str | None,
    harness: HarnessRef | None,
    counts: ItemCounts,
) -> list[str]:
    out: list[str] = []
    if dataset.resolved_sha is None:
        out.append(
            "The dataset revision was not resolved against the Hub: the repo id, config and "
            "split are declared, not confirmed."
        )
    elif not dataset.pinned:
        out.append(
            f"The dataset was requested as '{dataset.requested_revision or 'default branch'}', "
            "a moving ref; it resolved to the recorded commit at resolved_at and may differ later."
        )
    if dataset.evidence_strength != "local_bytes_verified":
        out.append(
            "The Hub revision and the local dataset content hash were not cross-checked: no "
            "local file was hashed against a digest the Hub published."
        )
    if counts.unscored:
        out.append(
            f"{counts.unscored} of {counts.attempted} items produced no score (errored, skipped "
            "or unlabelled) and are excluded from the aggregate; the interval covers scored "
            "items only."
        )
    if 0 < counts.scored < SMALL_SAMPLE:
        out.append(f"Small sample (n={counts.scored}): the confidence interval is wide.")
    if model is None and model_declared is None:
        out.append(
            "No model reference was recorded; the receipt cannot say which model was scored."
        )
    elif model is None or model.resolved_sha is None:
        out.append(
            "The model is declared, not resolved to an immutable Hub revision; a Hub revision "
            "would not prove which weights a provider served either."
        )
    if harness is None:
        out.append("No evaluation harness or container digest was recorded.")
    elif harness.container_digest is None:
        out.append("The harness is named but not pinned by a container digest.")
    out.append(NOT_VERIFIED)
    return out


def build_eval_receipt(
    experiment: Experiment,
    dataset: HubReference,
    *,
    metric: str | None = None,
    model: HubReference | None = None,
    model_declared: str | None = None,
    harness: HarnessRef | None = None,
    template_digest: str | None = None,
    evidence_refs: Sequence[str] = (),
) -> EvalReceipt:
    """Bind a finalized *experiment* to a dataset (and optionally model) Hub reference.

    Raises :class:`EvalReceiptError` for an unfinalized experiment, a reference of the wrong
    type, a malformed digest, or a *metric* the experiment does not hold -- there is no path that
    yields a value the experiment did not compute.
    """
    if experiment.status != "finalized" or not experiment.content_hash:
        raise EvalReceiptError("only a finalized experiment can be receipted")
    if dataset.repo_type != "dataset":
        raise EvalReceiptError("the dataset reference must have repo_type 'dataset'")
    if model is not None and model.repo_type != "model":
        raise EvalReceiptError("the model reference must have repo_type 'model'")
    if harness is not None:
        _digest(harness.container_digest, "container_digest")
        _plain_text(harness.name, "harness.name")
        _plain_text(harness.version, "harness.version")
    _plain_text(model_declared, "model_declared")
    aggregates = [a for a in experiment.aggregate if metric is None or a.metric == metric]
    if metric is not None and not aggregates:
        raise EvalReceiptError(f"experiment {experiment.experiment_id} has no metric {metric!r}")
    primary = aggregates[0] if aggregates else None
    counts = _counts(experiment, primary)
    return EvalReceipt(
        experiment_id=experiment.experiment_id,
        experiment_content_hash=experiment.content_hash,
        dataset=dataset,
        local_dataset=experiment.dataset_ref.model_dump(),
        target_ref=experiment.target.ref,
        model=model,
        model_declared=model_declared,
        harness=harness,
        scorer_ref=experiment.score_config_ref,
        scorer_config_digest=experiment.score_config_digest,
        template_digest=_digest(template_digest, "template_digest"),
        evidence_refs=[_digest(ref, "evidence_ref") or "" for ref in evidence_refs],
        counts=counts,
        metrics=[_report(a) for a in aggregates],
        limitations=_limitations(dataset, model, model_declared, harness, counts),
    )


# ── Two-run comparison -------------------------------------------------------


def compare_receipts(
    baseline: EvalReceipt, candidate: EvalReceipt, comparison: ExperimentComparison
) -> dict[str, Any]:
    """Restate an existing :class:`ExperimentComparison` with both sides' uncertainty.

    The verdict, counts and per-item changes come from *comparison* (the existing ADR-0120 /
    ADR-0080 statistics, embedded verbatim); this adds the two receipts' limitations and a
    statement of what a difference does and does not show. It computes no statistic of its own
    beyond the arithmetic difference of two reported values.
    """
    metric = comparison.metric
    base = next((m for m in baseline.metrics if m.metric == metric), None)
    cand = next((m for m in candidate.metrics if m.metric == metric), None)
    if base is None or cand is None:
        raise EvalReceiptError(f"both receipts must hold metric {metric!r}")
    if baseline.experiment_id != comparison.comparison_of.baseline_experiment_id or (
        candidate.experiment_id != comparison.comparison_of.candidate_experiment_id
    ):
        raise EvalReceiptError("the receipts are not the experiments the comparison is over")
    unattributed = [
        label
        for label, receipt in (("baseline", baseline), ("candidate", candidate))
        if receipt.model is None or receipt.model.resolved_sha is None
    ]
    statement = [
        "A difference is an observation over the paired items; it does not identify why the "
        "outputs changed.",
        "'Different' is not 'worse': read the interval, the item counts and the changed items.",
    ]
    if unattributed:
        statement.append(
            "Model revision is not established for: "
            + ", ".join(unattributed)
            + ". Nothing here attributes the change to a model revision."
        )
    else:
        statement.append(
            "Both model references resolve to Hub revisions; provider routing, sampling and "
            "prompt differences can still change outputs."
        )
    return {
        "metric": metric,
        "baseline": base.model_dump(),
        "candidate": cand.model_dump(),
        "difference": cand.value - base.value,
        "regression_significant": comparison.is_regression(),
        "verdict": comparison.significance.get("sprt", {}).get("verdict"),
        "items_changed": sum(1 for item in comparison.per_item if item.changed),
        "items_paired": sum(
            1
            for item in comparison.per_item
            if item.baseline is not None and item.candidate is not None
        ),
        "statement": statement,
        "limitations": {"baseline": baseline.limitations, "candidate": candidate.limitations},
    }


# ── .eval_results export (opt-in, local, validated) --------------------------


def _is_https_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not any(ch.isspace() or ord(ch) < 32 for ch in value)
    )


def validate_eval_results(entries: object) -> list[str]:
    """Problems with *entries* against the documented ``.eval_results`` shape (empty = valid).

    Strict on purpose: unknown keys are errors, ``verifyToken`` is refused outright (only the
    Hub's own jobs mint one), and a ``source`` needs an https URL without credentials.
    """
    if not isinstance(entries, list) or not entries:
        return ["top level must be a non-empty list of result entries"]
    problems: list[str] = []
    for i, entry in enumerate(entries):
        where = f"entry {i}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        for key in sorted(set(entry) - _ENTRY_KEYS):
            problems.append(f"{where}: unknown key {key!r}")
        if "verifyToken" in entry:
            problems.append(f"{where}: verifyToken must not be set by NovaFabric")
        dataset = entry.get("dataset")
        if not isinstance(dataset, dict):
            problems.append(f"{where}: dataset must be a mapping")
        else:
            for key in sorted(set(dataset) - _DATASET_KEYS):
                problems.append(f"{where}: unknown dataset key {key!r}")
            if not isinstance(dataset.get("id"), str) or "/" not in str(dataset.get("id")):
                problems.append(f"{where}: dataset.id must be a Hub dataset id 'owner/name'")
            if not isinstance(dataset.get("task_id"), str) or not _TASK_ID_RE.match(
                str(dataset.get("task_id"))
            ):
                problems.append(f"{where}: dataset.task_id is required and must be a plain id")
            rev = dataset.get("revision")
            if rev is not None and (
                not isinstance(rev, str) or not re.match(r"^[0-9a-f]{40}\Z", rev)
            ):
                problems.append(f"{where}: dataset.revision must be a 40-hex commit")
        value = entry.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            problems.append(f"{where}: value must be a finite number")
        stamp = entry.get("date")
        if stamp is not None:
            try:
                if not isinstance(stamp, str):
                    raise ValueError
                datetime.fromisoformat(
                    stamp.replace("Z", "+00:00")
                ) if "T" in stamp else date.fromisoformat(stamp)
            except ValueError:
                problems.append(f"{where}: date must be an ISO-8601 date or datetime string")
        notes = entry.get("notes")
        if notes is not None and (not isinstance(notes, str) or len(notes) > _MAX_NOTES):
            problems.append(f"{where}: notes must be a string of at most {_MAX_NOTES} characters")
        source = entry.get("source")
        if source is not None:
            if not isinstance(source, dict):
                problems.append(f"{where}: source must be a mapping")
            else:
                for key in sorted(set(source) - _SOURCE_KEYS):
                    problems.append(f"{where}: unknown source key {key!r}")
                if not _is_https_url(source.get("url")):
                    problems.append(f"{where}: source.url must be an https URL without credentials")
                if "name" in source and not (
                    isinstance(source["name"], str) and _NAME_RE.match(source["name"])
                ):
                    problems.append(f"{where}: source.name is not a plain display name")
                for key in ("user", "org"):
                    if key in source and not (
                        isinstance(source[key], str) and _HANDLE_RE.match(source[key])
                    ):
                        problems.append(f"{where}: source.{key} is not a Hub handle")
    return problems


def to_eval_results(
    receipt: EvalReceipt,
    *,
    metric: str,
    task_id: str,
    source_url: str | None = None,
    source_name: str | None = None,
    source_user: str | None = None,
    source_org: str | None = None,
    on: str | None = None,
) -> list[dict[str, Any]]:
    """One ``.eval_results`` entry for *metric*, built only from the receipt's own numbers.

    *task_id* is the id the benchmark's ``eval.yaml`` defines; NovaFabric cannot know it, so the
    caller supplies it. ``revision`` is included only when the dataset was resolved to a commit.
    Raises :class:`EvalReceiptError` rather than emit an entry that fails
    :func:`validate_eval_results`.
    """
    report = next((m for m in receipt.metrics if m.metric == metric), None)
    if report is None:
        raise EvalReceiptError(f"the receipt holds no metric {metric!r}")
    dataset_id = receipt.dataset.canonical_repo_id or receipt.dataset.repo_id
    notes = _notes(receipt, report)
    entry: dict[str, Any] = {
        "dataset": {"id": dataset_id, "task_id": task_id},
        "value": report.value,
        "notes": notes,
    }
    if receipt.dataset.resolved_sha:
        entry["dataset"]["revision"] = receipt.dataset.resolved_sha
    if on:
        entry["date"] = on
    if source_url is not None:
        source: dict[str, str] = {"url": source_url}
        for key, val in (("name", source_name), ("user", source_user), ("org", source_org)):
            if val is not None:
                source[key] = val
        entry["source"] = source
    problems = validate_eval_results([entry])
    if problems:
        raise EvalReceiptError("; ".join(problems))
    return [entry]


def _notes(receipt: EvalReceipt, report: MetricReport) -> str:
    scale = (
        "fraction in [0, 1]" if report.value_type == "boolean" else "mean of the scorer's values"
    )
    parts = [
        "Recorded by NovaFabric, not a Hugging Face verified score.",
        f"{report.metric} {report.reducer} ({scale}), n={report.n} of "
        f"{receipt.counts.attempted} items.",
    ]
    if report.interval is not None:
        parts.append(f"95% Wilson interval [{report.interval[0]:.3f}, {report.interval[1]:.3f}].")
    parts.append(f"experiment {receipt.experiment_content_hash}")
    return " ".join(parts)[:_MAX_NOTES]


def render_eval_results_yaml(entries: Sequence[dict[str, Any]]) -> str:
    problems = validate_eval_results(list(entries))
    if problems:
        raise EvalReceiptError("; ".join(problems))
    return yaml.safe_dump(list(entries), sort_keys=False, allow_unicode=True)


def write_eval_results(
    entries: Sequence[dict[str, Any]], path: Path, *, overwrite: bool = False
) -> Path:
    """Write validated YAML to a *local* file inside a folder named ``.eval_results``.

    The folder-name rule keeps this from being pointed at an arbitrary file; refusing to
    overwrite keeps a hand-edited result safe. Nothing is uploaded: getting the file onto a
    model repo is a separate, human, approved step.
    """
    target = Path(path)
    if target.parent.name != ".eval_results" or target.suffix != ".yaml":
        raise EvalReceiptError("path must be <dir>/.eval_results/<name>.yaml")
    text = render_eval_results_yaml(entries)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise EvalReceiptError(f"{target} exists; pass overwrite=True to replace it")
    target.write_text(text, encoding="utf-8")
    return target


# ── Model / Dataset Card backlink (append-only, pure text) -------------------


def render_card_backlink(receipt: EvalReceipt, *, evidence_url: str | None = None) -> str:
    """The evidence section to append to a card. Receipt facts only; links only if https."""
    if evidence_url is not None and not _is_https_url(evidence_url):
        raise EvalReceiptError("evidence_url must be an https URL without credentials")
    lines = [
        CARD_BLOCK_BEGIN,
        "## Evaluation evidence (NovaFabric)",
        "",
        NOT_VERIFIED,
        "",
        f"- Dataset: `{receipt.dataset.repo_id}` "
        f"({receipt.dataset.evidence_strength.replace('_', ' ')})",
        f"- Experiment: `{receipt.experiment_content_hash}`",
    ]
    for m in receipt.metrics:
        band = f", 95% interval [{m.interval[0]:.3f}, {m.interval[1]:.3f}]" if m.interval else ""
        lines.append(f"- {m.metric}: {m.value:.4f} (n={m.n}{band})")
    if evidence_url:
        lines.append(f"- Evidence: {evidence_url}")
    lines += ["", "Limitations:", *(f"- {text}" for text in receipt.limitations), CARD_BLOCK_END]
    return "\n".join(lines) + "\n"


def append_card_backlink(card_text: str, section: str) -> str:
    """Add *section* to *card_text* without altering any other byte; replace only our own block.

    Front matter, licence and every human-written line stay exactly as they were. Applying the
    same section twice is a no-op, so a re-run never stacks duplicates.
    """
    if section.count(CARD_BLOCK_BEGIN) != 1 or section.count(CARD_BLOCK_END) != 1:
        raise EvalReceiptError("section must be a block from render_card_backlink")
    begin, end = card_text.find(CARD_BLOCK_BEGIN), card_text.find(CARD_BLOCK_END)
    if begin != -1 and end > begin:
        stop = end + len(CARD_BLOCK_END)
        if card_text[stop : stop + 1] == "\n":
            stop += 1
        return card_text[:begin] + section + card_text[stop:]
    if begin != -1 or end != -1:
        raise EvalReceiptError("the card holds a damaged NovaFabric evidence block; fix it by hand")
    separator = (
        ""
        if not card_text or card_text.endswith("\n\n")
        else ("\n" if card_text.endswith("\n") else "\n\n")
    )
    return card_text + separator + section
