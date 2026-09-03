"""ADR-0147 D2/D6 — the sealed-capsule collector for the drift detectors.

Every test here builds a **real capsule tree on disk** and reads it through the shipped ADR-0129
scanner. Mocking the scanner would test the aggregation and leave the thing most likely to be
wrong — whether this agrees with the rest of the codebase about what a capsule is and when it
falls inside a window — untested.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from novafabric.drift.collect import (
    NUMERIC_DIMENSIONS,
    CollectError,
    collect_runs,
    detect_document,
    fingerprint_document,
    read_trajectory,
    root_cause_document,
    runs_document,
    sample,
    silent_failure_document,
)

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _capsule(
    root: Path,
    run_id: str,
    *,
    created_at: str,
    status: str = "success",
    calls: list[dict] | None = None,
    scores: dict[str, float] | None = None,
) -> Path:
    d = root / run_id
    d.mkdir(parents=True)
    (d / "capsule.json").write_text(
        json.dumps({"run_id": run_id, "created_at": created_at, "status": status})
    )
    if calls:
        (d / "model-calls.jsonl").write_text(
            "\n".join(json.dumps(c) for c in calls) + "\n"
        )
    if scores:
        (d / "scores.jsonl").write_text(
            "\n".join(json.dumps(_score(n, v)) for n, v in scores.items()) + "\n"
        )
    return d


#: A valid ``scores.jsonl`` line — the shape `eval.scores.Score` enforces.
_SHA = "sha256:" + "a" * 64


def _score(name: str, value: float) -> dict:
    return {
        "score_id": "01HZZZZZZZZZZZZZZZZZZZZZZZ",
        "subject": _SHA,
        "subject_kind": "capsule",
        "name": name,
        "value": value,
        "value_type": "numeric",
        "source": "code",
        "evaluator_id": "test-evaluator",
        "eval_card_digest": _SHA,
    }


def _call(
    *,
    model: str = "gpt-x",
    cost: float | None = None,
    currency: str = "EUR",
    input_tokens: float | None = None,
    output_tokens: float | None = None,
    duration_ms: float | None = None,
) -> dict:
    record: dict = {"gen_ai.response.model": model}
    if cost is not None:
        record["nova.cost"] = {"amount": cost, "currency": currency}
    if input_tokens is not None:
        record["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        record["gen_ai.usage.output_tokens"] = output_tokens
    if duration_ms is not None:
        record["duration_ms"] = duration_ms
    return record


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(
        root, "run-a", created_at="2026-07-10T00:00:00Z",
        calls=[_call(cost=0.10, input_tokens=100, output_tokens=50, duration_ms=900),
               _call(cost=0.20, input_tokens=200, output_tokens=60, duration_ms=1100)],
        scores={"pass-rate": 0.9},
    )
    _capsule(
        root, "run-b", created_at="2026-07-11T00:00:00Z",
        calls=[_call(cost=0.30, input_tokens=300, output_tokens=70, duration_ms=1000)],
        scores={"pass-rate": 0.8},
    )
    # No model calls and no scores at all — the capsule still exists.
    _capsule(root, "run-c", created_at="2026-07-11T12:00:00Z", status="failure")
    return root


def _runs(store: Path, **kwargs):
    return collect_runs(store, now=NOW, use_cache=False, **kwargs)


# ── It reuses the ADR-0129 scanner, and agrees with it about the layout ───


def test_it_reads_the_shared_scanner_not_a_second_walker(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One definition of 'what is a capsule'; a second walker would eventually disagree."""
    import novafabric.drift.collect as mod

    calls: list[Path] = []
    real = mod.scan_capsule_dir_cached

    def spy(capsule_dir, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(Path(capsule_dir))
        return real(capsule_dir, **kwargs)

    monkeypatch.setattr(mod, "scan_capsule_dir_cached", spy)
    _runs(store)
    assert calls == [store]


def test_a_directory_without_a_manifest_is_not_a_capsule(store: Path) -> None:
    (store / "notes").mkdir()
    (store / "notes" / "model-calls.jsonl").write_text(json.dumps(_call(cost=99.0)) + "\n")
    assert {r.run_id for r in _runs(store)} == {"run-a", "run-b", "run-c"}


def test_the_capsules_are_not_written_to(store: Path) -> None:
    """Capsules are signed evidence."""
    before = {
        p: p.read_bytes() for p in sorted(store.rglob("*")) if p.is_file()
    }
    _runs(store)
    after = {p: p.read_bytes() for p in sorted(store.rglob("*")) if p.is_file()}
    assert before == after


# ── The window means what `nova query` means by it ───────────────────────


def test_the_window_is_half_open_since_inclusive_until_exclusive(store: Path) -> None:
    """Pinned rather than assumed — `nova query` filters `created_at >= since AND < until`."""
    on_since = _runs(store, since="2026-07-11T00:00:00Z", until="2026-07-11T12:00:00Z")
    assert [r.run_id for r in on_since] == ["run-b"], "since is inclusive, until is exclusive"


def test_a_relative_duration_window_works_like_the_query_dsl(store: Path) -> None:
    assert {r.run_id for r in _runs(store, since="3d")} == {"run-a", "run-b", "run-c"}
    # run-c is at NOW-24h exactly, so a 1d window includes it — `since` is inclusive on the
    # duration path too, not only on the timestamp path.
    assert {r.run_id for r in _runs(store, since="1d")} == {"run-c"}


def test_an_empty_window_is_zero_runs_not_an_error(store: Path) -> None:
    assert _runs(store, since="2020-01-01T00:00:00Z", until="2020-01-02T00:00:00Z") == []


def test_a_duration_in_until_is_refused_with_the_reason(store: Path) -> None:
    """`until` takes a timestamp only — inherited from `nova query`, and said so plainly.

    Found by running the documented example rather than by reading it: `--baseline 30d..7d` is
    the obvious thing to write and it does not work.
    """
    with pytest.raises(CollectError, match="`until` accepts a timestamp only"):
        _runs(store, since="30d", until="7d")


# ── Aggregation, and the missing-is-not-zero rule ────────────────────────


def test_a_runs_calls_are_summed(store: Path) -> None:
    run_a = next(r for r in _runs(store) if r.run_id == "run-a")
    assert run_a.model_calls == 2
    assert run_a.cost == pytest.approx(0.30)
    assert run_a.cost_calls == 2
    assert run_a.prompt_tokens == 300
    assert run_a.completion_tokens == 110
    assert run_a.total_tokens == 410
    assert run_a.latency == 2000
    assert run_a.scores == {"pass-rate": 0.9}


def test_a_run_with_no_calls_is_collected_with_none_not_zero(store: Path) -> None:
    run_c = next(r for r in _runs(store) if r.run_id == "run-c")
    assert run_c.model_calls == 0
    assert run_c.cost is None, "a run that recorded no cost did not cost zero"
    assert run_c.total_tokens is None
    assert run_c.status == "failure"


def test_a_partial_cost_sum_is_visible_as_partial(tmp_path: Path) -> None:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(
        root, "run-p", created_at="2026-07-11T00:00:00Z",
        calls=[_call(cost=0.10), _call(), _call()],
    )
    run = _runs(root)[0]
    assert run.cost == pytest.approx(0.10)
    assert (run.cost_calls, run.model_calls) == (1, 3), "1 of 3 calls carried cost"


def test_a_run_mixing_currencies_is_refused_not_summed(tmp_path: Path) -> None:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(
        root, "run-m", created_at="2026-07-11T00:00:00Z",
        calls=[_call(cost=1.0, currency="EUR"), _call(cost=2.0, currency="JPY")],
    )
    with pytest.raises(CollectError, match="more than one currency"):
        _runs(root)


# ── Sampling ─────────────────────────────────────────────────────────────


def test_a_missing_value_is_left_out_and_counted(store: Path) -> None:
    drawn = sample(_runs(store), "cost")
    assert drawn.values == pytest.approx([0.30, 0.30])
    assert (drawn.contributing, drawn.missing) == (2, 1), "run-c has no cost, and is not a zero"
    assert drawn.currency == "EUR"


def test_model_calls_counts_zero_as_a_real_value(store: Path) -> None:
    """Zero calls is a measurement; absent cost is not."""
    drawn = sample(_runs(store), "model-calls")
    assert sorted(drawn.values) == [0.0, 1.0, 2.0]
    assert drawn.missing == 0


def test_a_score_dimension_reads_the_named_score(store: Path) -> None:
    drawn = sample(_runs(store), "score:pass-rate")
    assert sorted(drawn.values) == pytest.approx([0.8, 0.9])
    assert (drawn.contributing, drawn.missing) == (2, 1)


def test_an_unknown_dimension_is_refused_not_silently_empty(store: Path) -> None:
    """An empty sample would read as 'nothing drifted'."""
    with pytest.raises(CollectError, match="unknown dimension"):
        sample(_runs(store), "cost-per-token")


@pytest.mark.parametrize("dimension", NUMERIC_DIMENSIONS)
def test_every_declared_dimension_is_actually_samplable(dimension: str, store: Path) -> None:
    """A vocabulary entry with no implementation would raise KeyError at the worst moment."""
    assert sample(_runs(store), dimension).dimension == dimension


def test_samples_mixing_currencies_across_runs_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(root, "run-e", created_at="2026-07-11T00:00:00Z", calls=[_call(cost=1.0, currency="EUR")])
    _capsule(root, "run-j", created_at="2026-07-11T01:00:00Z", calls=[_call(cost=2.0, currency="JPY")])
    with pytest.raises(CollectError, match="more than one currency"):
        sample(_runs(root), "cost")


# ── Documents ────────────────────────────────────────────────────────────


def test_a_score_dimension_becomes_output_drift_and_cost_becomes_behavioral(store: Path) -> None:
    """Derived, so a cost distribution cannot be filed as output-drift."""
    runs = _runs(store)
    assert detect_document(
        baseline=runs, window=runs, dimension="score:pass-rate", statistic="psi", threshold=0.2
    )["kind"] == "output"
    assert detect_document(
        baseline=runs, window=runs, dimension="cost", statistic="psi", threshold=0.2
    )["kind"] == "behavioral"


def test_the_detect_document_reports_what_it_left_out(store: Path) -> None:
    doc = detect_document(
        baseline=_runs(store), window=_runs(store), dimension="cost",
        statistic="psi", threshold=0.2,
    )
    assert doc["collected"]["window"] == {"runs": 3, "contributing": 2, "missing": 1}
    assert doc["collected"]["currency"] == "EUR"


def test_an_empty_sample_refuses_to_become_a_drift_document(store: Path) -> None:
    """A statistic over nothing is no evidence, not evidence of no drift."""
    empty = _runs(store, since="2020-01-01T00:00:00Z", until="2020-01-02T00:00:00Z")
    with pytest.raises(CollectError, match="no evidence"):
        detect_document(
            baseline=empty, window=_runs(store), dimension="cost",
            statistic="psi", threshold=0.2,
        )
    with pytest.raises(CollectError, match="no evidence"):
        detect_document(
            baseline=_runs(store), window=empty, dimension="cost",
            statistic="psi", threshold=0.2,
        )


def test_the_document_carries_no_verdict(store: Path) -> None:
    """It collects; the detector judges."""
    doc = detect_document(
        baseline=_runs(store), window=_runs(store), dimension="cost",
        statistic="psi", threshold=0.2,
    )
    assert "drifted" not in doc
    assert "value" not in doc


def test_a_run_without_the_quality_metric_is_excluded_and_counted(store: Path) -> None:
    doc = silent_failure_document(
        runs=_runs(store), quality_metric="pass-rate", threshold=0.85
    )
    assert [r["run_id"] for r in doc["runs"]] == ["run-a", "run-b"]
    assert doc["collected"] == {
        "schema_version": doc["collected"]["schema_version"],
        "quality_metric": "pass-rate",
        "runs": 3,
        "contributing": 2,
        "missing": 1,
    }


def test_a_metric_no_run_carries_is_refused(store: Path) -> None:
    """Far more often a typo than a finding — and it would score every run as absent."""
    with pytest.raises(CollectError, match="check the metric name"):
        silent_failure_document(runs=_runs(store), quality_metric="pss-rate", threshold=0.5)


def test_the_runs_document_counts_what_it_holds(store: Path) -> None:
    doc = runs_document(_runs(store), window={"since": None, "until": "2026-07-12T12:00:00Z"})
    assert doc["n"] == 3
    assert len(doc["runs"]) == 3


# ── The emitted documents are what the detectors consume ─────────────────


def test_the_detect_document_is_accepted_by_the_detector(store: Path) -> None:
    """Pins the two contracts together, so they cannot drift apart."""
    from novafabric.drift.detectors import build_behavioral_drift, build_output_drift

    behavioral = detect_document(
        baseline=_runs(store), window=_runs(store), dimension="cost",
        statistic="psi", threshold=0.2,
    )
    record = build_behavioral_drift(
        dimension=behavioral["dimension"], distance=behavioral["distance"],
        baseline=behavioral["baseline"], window=behavioral["window"],
        threshold=behavioral["threshold"],
    )
    assert record.drifted is False  # identical samples

    output = detect_document(
        baseline=_runs(store), window=_runs(store), dimension="score:pass-rate",
        statistic="psi", threshold=0.2, baseline_id="bl-1",
    )
    out_record = build_output_drift(
        metric=output["metric"], statistic=output["statistic"],
        baseline=output["baseline"], window=output["window"],
        threshold=output["threshold"], window_meta=output["window_meta"],
        baseline_id=output["baseline_id"],
    )
    assert out_record.drifted is False


def test_the_silent_failure_document_is_accepted_by_the_detector(store: Path) -> None:
    from novafabric.drift.silent_failure import detect_silent_failures

    doc = silent_failure_document(
        runs=_runs(store), quality_metric="pass-rate", threshold=0.85
    )
    report = detect_silent_failures(
        doc["runs"], threshold=doc["threshold"], success_statuses=doc["success_statuses"]
    )
    assert report.total_reported_success == 2
    assert report.silent_failures == 1  # run-b at 0.8 reported success


# ── Trajectories (slice 2, NF-155) ───────────────────────────────────────


def _tool_calls(capsule: Path, records: list[dict]) -> None:
    (capsule / "tool-calls.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )


def _call_record(name: str, **arguments) -> dict:
    return {"tool_name": name, "arguments": arguments}


def test_a_trajectory_is_read_in_recorded_order(tmp_path: Path) -> None:
    root = tmp_path / "capsules"
    root.mkdir()
    c = _capsule(root, "run-t", created_at="2026-07-11T00:00:00Z")
    _tool_calls(c, [_call_record("search", q="a"), _call_record("write", path="o")])
    assert read_trajectory(c) == [
        {"name": "search", "arguments": {"q": "a"}, "version": None, "schema_validation": None},
        {"name": "write", "arguments": {"path": "o"}, "version": None, "schema_validation": None},
    ]


def test_the_recorded_schema_verdict_is_carried(tmp_path: Path) -> None:
    """Additive for NF-168 — one strict reader of this file, not four."""
    root = tmp_path / "capsules"
    root.mkdir()
    c = _capsule(root, "run-sv", created_at="2026-07-11T00:00:00Z")
    _tool_calls(c, [{
        "tool_name": "search", "arguments": {},
        "schema_validation": {"arguments_valid": True, "result_valid": None},
    }])
    assert read_trajectory(c)[0]["schema_validation"] == {
        "arguments_valid": True, "result_valid": None,
    }


def test_the_recorded_tool_version_is_carried(tmp_path: Path) -> None:
    """Additive for NF-169, so this file has one strict reader rather than two."""
    root = tmp_path / "capsules"
    root.mkdir()
    c = _capsule(root, "run-v", created_at="2026-07-11T00:00:00Z")
    _tool_calls(c, [{"tool_name": "search", "arguments": {}, "tool_version": "1.2.0"}])
    assert read_trajectory(c)[0]["version"] == "1.2.0"


def test_the_extra_version_key_does_not_disturb_the_fingerprint(tmp_path: Path) -> None:
    """`fingerprint_run` reads name and arguments and ignores the rest — asserted, not assumed."""
    from novafabric.drift.fingerprint import fingerprint_run

    with_version = [{
        "name": "search", "arguments": {"q": "a"}, "version": "1.2.0",
        "schema_validation": {"arguments_valid": True},
    }]
    without = [{"name": "search", "arguments": {"q": "a"}}]
    assert (
        fingerprint_run("r", with_version).signature
        == fingerprint_run("r", without).signature
    )


def test_a_capsule_with_no_tool_calls_has_an_empty_trajectory(store: Path) -> None:
    """An agent that called no tools is a real observation, not an error."""
    from novafabric.query.indexer import find_capsule

    capsule = find_capsule(store, "run-c")
    assert capsule is not None
    assert read_trajectory(capsule) == []


def test_a_malformed_line_is_refused_rather_than_skipped(tmp_path: Path) -> None:
    """The tolerant reader next door would accept this file — the difference is deliberate.

    Dropping a step still yields a perfectly valid-looking fingerprint, wrong in a way nothing
    downstream can detect.
    """
    root = tmp_path / "capsules"
    root.mkdir()
    c = _capsule(root, "run-bad", created_at="2026-07-11T00:00:00Z")
    (c / "tool-calls.jsonl").write_text(
        json.dumps(_call_record("search", q="a")) + "\n{not json\n"
    )

    # The conformance reader accepts it, dropping the bad line silently.
    from novafabric.capture.schema_validation import _read_tool_calls

    assert len(_read_tool_calls(c)) == 1

    with pytest.raises(CollectError, match="invalid tool-call record"):
        read_trajectory(c)


def test_a_record_without_a_tool_name_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "capsules"
    root.mkdir()
    c = _capsule(root, "run-noname", created_at="2026-07-11T00:00:00Z")
    _tool_calls(c, [{"arguments": {"q": "a"}}])
    with pytest.raises(CollectError, match="no 'tool_name'"):
        read_trajectory(c)


@pytest.fixture()
def traj_store(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    a = _capsule(root, "run-new", created_at="2026-07-11T00:00:00Z", scores={"pass-rate": 0.7})
    _tool_calls(a, [_call_record("search", q="a"), _call_record("deploy", env="prod")])
    b = _capsule(root, "golden", created_at="2026-07-01T00:00:00Z", scores={"pass-rate": 0.9})
    _tool_calls(b, [_call_record("search", q="a"), _call_record("write", path="o")])
    return root


def test_the_fingerprint_document_carries_both_trajectories(traj_store: Path) -> None:
    doc = fingerprint_document(
        traj_store, run_id="run-new", baseline_run_id="golden", threshold=0.2
    )
    assert [c["name"] for c in doc["run"]["calls"]] == ["search", "deploy"]
    assert [c["name"] for c in doc["baseline"]["calls"]] == ["search", "write"]
    assert doc["threshold"] == 0.2


def test_scores_are_opt_in_not_folded_in_silently(traj_store: Path) -> None:
    """Including whatever scores exist would change the basis — and the signature — unasked."""
    without = fingerprint_document(traj_store, run_id="run-new")
    assert "scores" not in without["run"]

    with_scores = fingerprint_document(
        traj_store, run_id="run-new", quality_metric="pass-rate"
    )
    assert with_scores["run"]["scores"] == [0.7]


def test_an_out_of_range_score_is_reported_not_dropped(tmp_path: Path) -> None:
    root = tmp_path / "capsules"
    root.mkdir()
    c = _capsule(root, "run-big", created_at="2026-07-11T00:00:00Z", scores={"likert": 4.0})
    _tool_calls(c, [_call_record("search", q="a")])
    with pytest.raises(CollectError, match=r"outside the \[0, 1\]"):
        fingerprint_document(root, run_id="run-big", quality_metric="likert")


def test_a_run_with_nothing_to_fingerprint_is_refused(store: Path) -> None:
    with pytest.raises(CollectError, match="nothing to fingerprint"):
        fingerprint_document(store, run_id="run-c")


def test_an_unknown_run_is_named_in_the_error(traj_store: Path) -> None:
    with pytest.raises(CollectError, match="no capsule for target run 'run-nope'"):
        fingerprint_document(traj_store, run_id="run-nope")


def test_a_baseline_without_a_threshold_is_refused(traj_store: Path) -> None:
    """A hidden default would make the caller's policy look like a property of the data."""
    with pytest.raises(CollectError, match="needs a --threshold"):
        fingerprint_document(traj_store, run_id="run-new", baseline_run_id="golden")


def test_the_document_is_accepted_by_the_fingerprint(traj_store: Path) -> None:
    """Pins the contract by running it, not by asserting a shape."""
    from novafabric.drift.fingerprint import compare_fingerprints, fingerprint_run

    doc = fingerprint_document(
        traj_store, run_id="run-new", baseline_run_id="golden", threshold=0.2,
        quality_metric="pass-rate",
    )
    target = fingerprint_run(
        doc["run"]["run_id"], doc["run"]["calls"], scores=doc["run"].get("scores")
    )
    base = fingerprint_run(
        doc["baseline"]["run_id"], doc["baseline"]["calls"], scores=doc["baseline"].get("scores")
    )
    comparison = compare_fingerprints(target, base, threshold=doc["threshold"])
    assert comparison.shifted is True
    assert comparison.distance is not None and comparison.distance > 0


# ── Provenance (slice 3, NF-157) ─────────────────────────────────────────


@pytest.fixture()
def lineage(tmp_path: Path):
    """A lineage store with two runs whose bound model differs."""
    from novafabric.lineage._store import LineageStore
    from novafabric.lineage._types import LineageEdge

    store = LineageStore(tmp_path / "lineage.db")
    for run, model in (("run-base", "gpt-4o-2024-11-20"), ("run-drift", "gpt-4o-2025-03-01")):
        for kind, ref in (("model", model), ("prompt", "prompt-v1")):
            store.insert_edge(
                LineageEdge(
                    edge_type="used",
                    # A run node's ref comes from `run_id`, not `ref` — `node_ref_from_edge_dict`
                    # is the single definition of node identity (ADR-0266).
                    source={"kind": "run", "run_id": run},
                    target={"kind": kind, "ref": ref},
                    confidence="declared",
                    capsule_run_id=run,
                )
            )
    return store


def test_the_document_carries_both_runs_ancestors(lineage) -> None:
    doc = root_cause_document(lineage, baseline_run="run-base", drifted_run="run-drift")
    assert {(n["kind"], n["ref"]) for n in doc["baseline"]} == {
        ("model", "gpt-4o-2024-11-20"),
        ("prompt", "prompt-v1"),
    }
    assert {(n["kind"], n["ref"]) for n in doc["drifted"]} == {
        ("model", "gpt-4o-2025-03-01"),
        ("prompt", "prompt-v1"),
    }


def test_a_run_absent_from_the_graph_is_refused(lineage) -> None:
    """Otherwise two empty ancestor lists report `no_change` — a finding invented from nothing.

    The second half of this test is the point: fed the empty lists the collector refuses to
    produce, the detector really does report "nothing changed".
    """
    with pytest.raises(CollectError, match="not in the lineage graph"):
        root_cause_document(lineage, baseline_run="run-base", drifted_run="run-ghost")
    with pytest.raises(CollectError, match="not in the lineage graph"):
        root_cause_document(lineage, baseline_run="run-ghost", drifted_run="run-drift")

    from novafabric.drift.root_cause import find_root_cause

    assert find_root_cause([], []).confidence == "no_change"


def test_the_ancestor_counts_and_depth_travel_with_the_document(lineage) -> None:
    """A walk too shallow to reach the change looks exactly like no change."""
    doc = root_cause_document(lineage, baseline_run="run-base", drifted_run="run-drift", depth=3)
    assert doc["collected"]["depth"] == 3
    assert doc["collected"]["baseline_ancestors"] == 2
    assert doc["collected"]["drifted_ancestors"] == 2


def test_kinds_are_passed_through_only_when_given(lineage) -> None:
    assert "kinds" not in root_cause_document(
        lineage, baseline_run="run-base", drifted_run="run-drift"
    )
    doc = root_cause_document(
        lineage, baseline_run="run-base", drifted_run="run-drift", kinds=["model"]
    )
    assert doc["kinds"] == ["model"]


def test_the_document_is_accepted_by_the_root_cause_detector(lineage) -> None:
    """Pins the contract by running it, not by asserting a shape."""
    from novafabric.drift.root_cause import find_root_cause

    doc = root_cause_document(lineage, baseline_run="run-base", drifted_run="run-drift")
    hypothesis = find_root_cause(doc["baseline"], doc["drifted"])
    assert hypothesis.confidence == "sole_change"
    assert [c.kind for c in hypothesis.changes] == ["model"]
    assert hypothesis.correlation_only is True


def test_has_node_distinguishes_absent_from_ancestorless(lineage) -> None:
    """The distinction the whole slice rests on, asserted directly."""
    assert lineage.has_node("run-base") is True
    assert lineage.has_node("run-ghost") is False
    # A leaf node is present and has no ancestors — both facts must be observable.
    assert lineage.has_node("prompt-v1") is True
    assert lineage.provenance("prompt-v1") == []
