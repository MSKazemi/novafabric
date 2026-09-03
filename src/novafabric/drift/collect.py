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

"""The sealed-capsule collector for the drift detectors (ADR-0147 D2/D6).

Until this existed, every ``nova drift`` subcommand took a **hand-written JSON document** — the
detectors were real, but the samples had to be transcribed out of capsules by a human. This reads
them from the sealed capsules directly, over a declared time window.

**It adds no new capsule reader.** ``query/indexer.py`` (ADR-0129) already defines what *is* a
capsule — *every immediate subdirectory carrying a ``capsule.yaml``/``capsule.json`` manifest* —
and already extracts model calls and scores, with an ADR-0225 cache in front of it. A second
walker would be a second definition of the same thing, and the two would eventually disagree
about which directories count. So this module aggregates the rows that scanner produces into
per-run records, and shapes those into the documents the detectors already consume. The window
means what ``nova query`` means by it, for the same reason: ``resolve_time_window`` is reused.

**It collects; it does not judge.** No ``drifted`` flag or verdict is computed here — that is the
detector's job, and ``threshold``/``statistic`` are required inputs rather than defaults because
they are the caller's policy. Capsules are signed evidence and nothing here writes to them.

Three rules keep the numbers honest, each of which has a way of failing quietly:

- **A missing value is not a zero.** A run that recorded no cost is *absent* from a cost sample
  rather than a ``0.0`` in it, and every document reports how many runs contributed and how many
  were missing. Averaging absent runs in as zero drags the distribution toward a drift that did
  not happen.
- **Mixed currencies are refused, not summed.** EUR minor units added to JPY minor units is a
  number with no meaning (the rule NF-154 already applies to the impact report).
- **An empty sample cannot become a drift document.** A statistic computed over nothing is not
  "no drift"; it is no evidence, and the two must not serialise the same way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from novafabric.query.cache import scan_capsule_dir_cached
from novafabric.query.errors import QueryError
from novafabric.query.executor import resolve_time_window
from novafabric.query.indexer import CallRow, ScoreRow, find_capsule

SCHEMA_VERSION = "0.1.0"

#: The numeric dimensions a run record can be sampled on. Closed, because a typo'd dimension must
#: not silently produce an empty sample that reads as "nothing drifted".
NUMERIC_DIMENSIONS: tuple[str, ...] = (
    "cost",
    "prompt-tokens",
    "completion-tokens",
    "total-tokens",
    "latency",
    "model-calls",
)

#: Prefix for a per-run eval score, e.g. ``score:pass-rate``.
SCORE_PREFIX = "score:"

#: The per-run trajectory log inside a capsule (ADR-0128 ``schemas/tool-call.schema.json``).
TOOL_CALLS_FILENAME = "tool-calls.jsonl"


class CollectError(ValueError):
    """Raised when capsules cannot be collected into an honest sample."""


class RunRecord(BaseModel):
    """One run, aggregated from its capsule's model calls and scores.

    Every numeric field is ``None`` when the capsule recorded nothing for it — never ``0.0``.
    ``cost_calls`` travels with ``cost`` so a *partial* sum is visible as one: a run whose 10
    calls include 2 with cost data has a cost, and it is not the run's cost.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    created_at: str
    status: str | None = None
    model: str | None = None
    model_calls: int = 0
    cost: float | None = None
    cost_currency: str | None = None
    #: How many of this run's model calls carried cost data.
    cost_calls: int = 0
    prompt_tokens: float | None = None
    completion_tokens: float | None = None
    total_tokens: float | None = None
    latency: float | None = None
    scores: dict[str, float] = Field(default_factory=dict)


class Sample(BaseModel):
    """Values for one dimension over a set of runs, with what was left out.

    ``missing`` is not decoration: a sample of 3 values drawn from 40 runs describes those 3 runs,
    and a reader who cannot see that will read it as describing all 40.
    """

    model_config = ConfigDict(frozen=True)

    dimension: str
    values: list[float]
    contributing: int
    missing: int
    #: Present only for a cost sample, so a currency is never implied by absence.
    currency: str | None = None


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ── Collecting run records ────────────────────────────────────────────────


def _sum_optional(values: Sequence[float | None]) -> tuple[float | None, int]:
    """Sum the values that are present. Returns ``(total, n_present)``.

    ``None`` when nothing was present — *not* ``0.0``, which would be indistinguishable from a
    run that genuinely cost nothing.
    """
    present = [v for v in values if v is not None]
    return (sum(present), len(present)) if present else (None, 0)


def _is_placeholder(call: CallRow) -> bool:
    """Is this the indexer's synthetic zero-call row rather than a recorded model call?"""
    return (
        call.model is None
        and call.model_id is None
        and call.cost is None
        and call.prompt_tokens is None
        and call.completion_tokens is None
        and call.total_tokens is None
        and call.latency is None
    )


def _record_for(run_id: str, calls: Sequence[CallRow], scores: Sequence[ScoreRow]) -> RunRecord:
    currencies = {c.cost_currency for c in calls if c.cost is not None and c.cost_currency}
    if len(currencies) > 1:
        raise CollectError(
            f"run {run_id!r} records costs in more than one currency ({sorted(currencies)}); "
            "summing them would produce a number with no meaning"
        )

    cost, cost_calls = _sum_optional([c.cost for c in calls])
    prompt, _ = _sum_optional([c.prompt_tokens for c in calls])
    completion, _ = _sum_optional([c.completion_tokens for c in calls])
    total, _ = _sum_optional([c.total_tokens for c in calls])
    latency, _ = _sum_optional([c.latency for c in calls])

    # The indexer emits one synthetic placeholder row for a capsule with no model calls, so a
    # run is not missing from the collection merely because it never called a model. The
    # placeholder is the row that carries *nothing*; a model call recording literally no model,
    # no cost, no tokens and no latency is indistinguishable from it, and carries no information
    # to distinguish anyway.
    real_calls = [c for c in calls if not _is_placeholder(c)]

    first = calls[0]
    return RunRecord(
        run_id=run_id,
        created_at=_iso(first.created_at),
        status=first.status,
        model=first.model,
        model_calls=len(real_calls),
        cost=cost,
        cost_currency=next(iter(currencies), None) if cost is not None else None,
        cost_calls=cost_calls,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        latency=latency,
        scores={s.name: s.value for s in scores},
    )


def collect_runs(
    capsule_dir: str | Path,
    *,
    since: str | None = None,
    until: str | None = None,
    now: datetime | None = None,
    use_cache: bool = True,
) -> list[RunRecord]:
    """Collect per-run records from the sealed capsules under *capsule_dir*.

    The window is half-open — ``created_at >= since`` and ``created_at < until`` — which is what
    ``nova query`` means by ``since``/``until``, because both resolve it through the same
    :func:`~novafabric.query.executor.resolve_time_window`.

    ``since`` accepts a duration (``7d``) **or** a timestamp; ``until`` accepts a **timestamp
    only**, and an absent ``until`` means *now*. That asymmetry is inherited on purpose: it is
    what ``nova query`` does, and accepting a duration here that ``nova query`` rejects would
    make the two mean different things by the same words.

    An empty window is **not** an error: zero runs is a legitimate answer, and it is the
    document-building step that refuses to turn zero samples into a drift record.

    Raises:
        CollectError: if a run records costs in more than one currency.
    """
    now = now or datetime.now(timezone.utc)
    try:
        since_epoch, until_epoch, _, _ = resolve_time_window(since, until, now)
    except QueryError as exc:
        # The asymmetry is real and inherited deliberately: this window is resolved by the same
        # code `nova query` uses, so accepting a duration here that `nova query` rejects would
        # make the two mean different things by the same words.
        raise CollectError(
            f"invalid window: {exc}. `since` accepts a duration (`7d`) or a timestamp; "
            "`until` accepts a timestamp only — the same rule `nova query` follows"
        ) from exc
    rows = scan_capsule_dir_cached(capsule_dir, use_cache=use_cache)

    def in_window(created_at: float) -> bool:
        return created_at < until_epoch and (since_epoch is None or created_at >= since_epoch)

    calls_by_run: dict[str, list[CallRow]] = {}
    for call in rows.calls:
        if in_window(call.created_at):
            calls_by_run.setdefault(call.run_id, []).append(call)
    scores_by_run: dict[str, list[ScoreRow]] = {}
    for score in rows.scores:
        if in_window(score.created_at):
            scores_by_run.setdefault(score.run_id, []).append(score)

    return [
        _record_for(run_id, calls, scores_by_run.get(run_id, []))
        for run_id, calls in sorted(calls_by_run.items())
    ]


# ── Sampling one dimension ────────────────────────────────────────────────


def _value_of(run: RunRecord, dimension: str) -> float | None:
    if dimension.startswith(SCORE_PREFIX):
        return run.scores.get(dimension[len(SCORE_PREFIX) :])
    if dimension == "model-calls":
        return float(run.model_calls)
    return {
        "cost": run.cost,
        "prompt-tokens": run.prompt_tokens,
        "completion-tokens": run.completion_tokens,
        "total-tokens": run.total_tokens,
        "latency": run.latency,
    }[dimension]


def sample(runs: Sequence[RunRecord], dimension: str) -> Sample:
    """Draw one dimension's values from *runs*, reporting what was missing.

    Runs with no value for the dimension are **left out and counted**, never defaulted to zero.

    Raises:
        CollectError: if *dimension* is not a known numeric dimension or a ``score:<name>``.
    """
    if dimension not in NUMERIC_DIMENSIONS and not dimension.startswith(SCORE_PREFIX):
        raise CollectError(
            f"unknown dimension {dimension!r}; known dimensions are "
            f"{list(NUMERIC_DIMENSIONS)} or '{SCORE_PREFIX}<name>'. A dimension that silently "
            "sampled nothing would read as 'nothing drifted'"
        )
    values = [v for v in (_value_of(r, dimension) for r in runs) if v is not None]
    currency = None
    if dimension == "cost":
        currencies = {r.cost_currency for r in runs if r.cost is not None and r.cost_currency}
        if len(currencies) > 1:
            raise CollectError(
                f"the runs record costs in more than one currency ({sorted(currencies)}); "
                "a distribution mixing them has no meaning — collect them separately"
            )
        currency = next(iter(currencies), None)
    return Sample(
        dimension=dimension,
        values=[float(v) for v in values],
        contributing=len(values),
        missing=len(runs) - len(values),
        currency=currency,
    )


# ── Trajectories ──────────────────────────────────────────────────────────


def read_trajectory(capsule: Path) -> list[dict[str, Any]]:
    """Read one capsule's tool calls as ``{name, arguments, version, schema_validation}``.

    **A malformed line is refused, never skipped.** ``capture/schema_validation`` reads the same
    file tolerantly, and is right to: one bad line should not fail a conformance *summary*. Here
    it would be a silent corruption of the measurement — a trajectory missing a step still looks
    like a perfectly good trajectory, and the fingerprint computed from it is wrong in a way
    nothing downstream can detect. This follows the indexer's rule instead (ADR-0129 D3): a
    clear error, never a silent wrong answer.

    A capsule with no ``tool-calls.jsonl`` has an empty trajectory, which is not an error — an
    agent that called no tools is a real observation.

    ``version`` carries the record's ``tool_version`` (required by
    ``schemas/tool-call.schema.json``, and *"Semver if known; 'unknown' otherwise"*);
    ``schema_validation`` carries the ADR-0128 verdict **as recorded**, or ``None`` when the call
    declared no schema. Both are additive:
    :func:`~novafabric.drift.fingerprint.fingerprint_run` reads ``name`` and ``arguments`` and
    ignores the rest, so the fingerprint is unchanged by their presence. They exist so NF-169 and
    NF-168 do not each add another reader of this file — the strictness above is the reason to
    keep one.
    """
    path = capsule / TOOL_CALLS_FILENAME
    if not path.is_file():
        return []
    calls: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise CollectError(
                f"{path}:{line_no}: invalid tool-call record ({exc}). Skipping it would drop a "
                "step from the trajectory, and a fingerprint missing a step still looks valid"
            ) from exc
        if not isinstance(record, dict) or not record.get("tool_name"):
            raise CollectError(
                f"{path}:{line_no}: tool-call record has no 'tool_name'; it cannot be placed in "
                "the trajectory, and dropping it would silently change the signature"
            )
        arguments = record.get("arguments")
        version = record.get("tool_version")
        verdict = record.get("schema_validation")
        calls.append(
            {
                "name": str(record["tool_name"]),
                "arguments": arguments if isinstance(arguments, dict) else {},
                "version": str(version) if version is not None else None,
                "schema_validation": verdict if isinstance(verdict, dict) else None,
            }
        )
    return calls


def fingerprint_document(
    capsule_dir: str | Path,
    *,
    run_id: str,
    baseline_run_id: str | None = None,
    threshold: float | None = None,
    quality_metric: str | None = None,
    commutable: Sequence[str] = (),
    idempotent: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the document ``nova drift fingerprint`` consumes, from sealed capsules.

    Scores are **opt-in** through *quality_metric*: a fingerprint over a trajectory alone is a
    complete answer, and silently folding in whatever scores happened to be present would change
    the basis — and therefore the signature — without the caller asking.

    Raises:
        CollectError: if a named run has no capsule, if a run has neither trajectory nor score
            (there is nothing to fingerprint, and a signature over nothing compares equal to
            every other nothing), or if a named score is outside the ``[0, 1]`` the fingerprint
            requires. An out-of-range score is reported, never dropped to make the document valid.
    """

    def block(rid: str, what: str) -> dict[str, Any]:
        capsule = find_capsule(capsule_dir, rid)
        if capsule is None:
            raise CollectError(f"no capsule for {what} run {rid!r} under {capsule_dir}")
        calls = read_trajectory(capsule)
        out: dict[str, Any] = {"run_id": rid, "calls": calls}
        if quality_metric is not None:
            _, scores = _rows_for(capsule)
            values = [s.value for s in scores if s.name == quality_metric]
            for value in values:
                if not 0.0 <= value <= 1.0:
                    raise CollectError(
                        f"{what} run {rid!r} scores {quality_metric!r} at {value}, outside the "
                        "[0, 1] a behavioral fingerprint requires — rescale it rather than "
                        "letting it be dropped to make the document valid"
                    )
            if values:
                out["scores"] = values
        if not calls and not out.get("scores"):
            lacking = (
                f" and no {quality_metric!r} score"
                if quality_metric
                else " and no scores were requested"
            )
            raise CollectError(
                f"{what} run {rid!r} has no tool calls{lacking}; there is nothing to "
                "fingerprint, and a signature over nothing compares equal to every other nothing"
            )
        return out

    doc: dict[str, Any] = {"run": block(run_id, "target")}
    if baseline_run_id is not None:
        doc["baseline"] = block(baseline_run_id, "baseline")
        if threshold is None:
            raise CollectError(
                "a baseline needs a --threshold: what counts as a behavioural shift is your "
                "policy, and a hidden default would make it look like a property of the data"
            )
        doc["threshold"] = threshold
    if commutable:
        doc["commutable"] = list(commutable)
    if idempotent:
        doc["idempotent"] = list(idempotent)
    doc["collected"] = {"schema_version": SCHEMA_VERSION, "capsules": str(capsule_dir)}
    return doc


# ── Detector-ready documents ──────────────────────────────────────────────


def _kind_for(dimension: str) -> str:
    """Which drift record a dimension belongs to.

    Derived rather than asked for: NF-151 output-drift is about the *distribution of what the
    agent produced* (its scores), while NF-152 behavioral-drift is about cost, tokens, latency
    and trajectory length. Letting a caller label a cost sample as output-drift would file the
    evidence under a claim it does not support.
    """
    return "output" if dimension.startswith(SCORE_PREFIX) else "behavioral"


def detect_document(
    *,
    baseline: Sequence[RunRecord],
    window: Sequence[RunRecord],
    dimension: str,
    statistic: str,
    threshold: float,
    baseline_id: str | None = None,
) -> dict[str, Any]:
    """Build the document ``nova drift detect`` consumes, for one dimension.

    The ``kind`` is derived from the dimension (see :func:`_kind_for`), so a cost distribution
    cannot be filed as output-drift.

    Raises:
        CollectError: if either side samples to nothing. A statistic over an empty sample is not
            "no drift" — it is no evidence, and the two must not serialise the same way.
    """
    base_sample = sample(baseline, dimension)
    window_sample = sample(window, dimension)
    for name, drawn, source in (
        ("baseline", base_sample, baseline),
        ("window", window_sample, window),
    ):
        if not drawn.values:
            raise CollectError(
                f"the {name} has no value for {dimension!r} across {len(source)} run(s), so no "
                "drift statistic can be computed. An empty sample is no evidence, not evidence "
                "of no drift"
            )

    kind = _kind_for(dimension)
    doc: dict[str, Any] = {
        "kind": kind,
        "statistic" if kind == "output" else "distance": statistic,
        "baseline": base_sample.values,
        "window": window_sample.values,
        "threshold": threshold,
        # Not consumed by the detector — carried so a reader can see what the samples describe.
        "collected": {
            "schema_version": SCHEMA_VERSION,
            "dimension": dimension,
            "currency": base_sample.currency,
            "baseline": {
                "runs": len(baseline),
                "contributing": base_sample.contributing,
                "missing": base_sample.missing,
            },
            "window": {
                "runs": len(window),
                "contributing": window_sample.contributing,
                "missing": window_sample.missing,
            },
        },
    }
    if kind == "output":
        doc["metric"] = dimension
        doc["window_meta"] = {
            "from": window[0].created_at if window else None,
            "to": window[-1].created_at if window else None,
            "run_ids": [r.run_id for r in window],
        }
        if baseline_id is not None:
            doc["baseline_id"] = baseline_id
    else:
        doc["dimension"] = dimension
    return doc


def silent_failure_document(
    *,
    runs: Sequence[RunRecord],
    quality_metric: str,
    threshold: float,
    success_statuses: Sequence[str] = ("success",),
) -> dict[str, Any]:
    """Build the document ``nova drift silent-failure`` consumes.

    A run with no ``quality_metric`` score is **excluded and counted**, never given a defaulted
    signal: silent-failure would otherwise flag — or clear — a run on a number nobody recorded.

    Raises:
        CollectError: if no run carries the metric at all, which is a naming mistake far more
            often than it is a finding.
    """
    included = [r for r in runs if quality_metric in r.scores]
    if not included:
        raise CollectError(
            f"no run in the window carries a {quality_metric!r} score "
            f"(of {len(runs)} run(s)); check the metric name — scoring every run as absent is "
            "not the same as scoring every run as poor"
        )
    return {
        "threshold": threshold,
        "success_statuses": list(success_statuses),
        "runs": [
            {
                "run_id": r.run_id,
                "status": r.status or "unknown",
                "quality_signal": r.scores[quality_metric],
            }
            for r in included
        ],
        "collected": {
            "schema_version": SCHEMA_VERSION,
            "quality_metric": quality_metric,
            "runs": len(runs),
            "contributing": len(included),
            "missing": len(runs) - len(included),
        },
    }


def root_cause_document(
    store: Any,
    *,
    baseline_run: str,
    drifted_run: str,
    kinds: Sequence[str] | None = None,
    depth: int = 5,
) -> dict[str, Any]:
    """Build the document ``nova drift root-cause`` consumes, from the lineage store.

    *store* is anything with ``has_node`` and ``provenance`` — the local
    :class:`~novafabric.lineage._store.LineageStore` in practice. Nothing is written to it.

    **A run absent from the graph is refused.** ``provenance()`` returns an empty list both for a
    ref that has no ancestors *and* for a ref that is not in the graph at all, and
    :func:`~novafabric.drift.root_cause.find_root_cause` reads two empty lists as
    ``confidence: no_change`` — *"nothing changed between these runs"*. That is a finding
    manufactured out of missing data, and nothing downstream can tell it from a real one.

    A run that *is* present with zero ancestors is fine, and the ancestor counts travel with the
    document so "we recorded no ancestors" cannot be read as "the provenance is identical".

    The ``depth`` used is recorded for the same reason: a walk too shallow to reach the change
    that happened looks exactly like no change.

    Raises:
        CollectError: if either run is absent from the lineage graph.
    """
    for label, ref in (("baseline", baseline_run), ("drifted", drifted_run)):
        if not store.has_node(ref):
            raise CollectError(
                f"{label} run {ref!r} is not in the lineage graph. Collecting it as an empty "
                "ancestor list would make the two runs compare equal and report 'no change', "
                "which is a finding invented from missing data"
            )

    def ancestors(ref: str) -> list[dict[str, Any]]:
        return [
            {"kind": row["kind"], "ref": row["ref"]}
            for row in store.provenance(ref, depth=depth)
            if row.get("kind") and row.get("ref")
        ]

    baseline_nodes = ancestors(baseline_run)
    drifted_nodes = ancestors(drifted_run)
    doc: dict[str, Any] = {"baseline": baseline_nodes, "drifted": drifted_nodes}
    if kinds:
        doc["kinds"] = list(kinds)
    doc["collected"] = {
        "schema_version": SCHEMA_VERSION,
        "depth": depth,
        "baseline_run": baseline_run,
        "drifted_run": drifted_run,
        "baseline_ancestors": len(baseline_nodes),
        "drifted_ancestors": len(drifted_nodes),
    }
    return doc


def runs_document(runs: Sequence[RunRecord], *, window: Mapping[str, str | None]) -> dict[str, Any]:
    """The neutral per-run record list — the input every other document is shaped from."""
    return {
        "schema_version": SCHEMA_VERSION,
        "window": dict(window),
        "n": len(runs),
        "runs": [r.model_dump(exclude_none=True) for r in runs],
    }


def _rows_for(capsule: Path) -> tuple[list[CallRow], list[ScoreRow]]:
    """One capsule's rows, through the shared scanner rather than a second reader."""
    from novafabric.query.indexer import scan_capsule

    scanned = scan_capsule(capsule)
    if scanned is None:  # pragma: no cover - find_capsule only returns manifest-bearing dirs
        raise CollectError(f"{capsule} carries no capsule manifest")
    return scanned


__all__ = [
    "NUMERIC_DIMENSIONS",
    "SCHEMA_VERSION",
    "SCORE_PREFIX",
    "TOOL_CALLS_FILENAME",
    "CollectError",
    "RunRecord",
    "Sample",
    "collect_runs",
    "detect_document",
    "fingerprint_document",
    "read_trajectory",
    "root_cause_document",
    "runs_document",
    "sample",
    "silent_failure_document",
]
