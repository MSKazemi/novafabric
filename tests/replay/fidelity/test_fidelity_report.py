"""The per-surface fidelity report cannot overstate (issue #59).

These tests need no capture and no network: they feed ``_report`` and ``classify``
synthetic observations (built from the harness's own dataclasses, so the JSON shape is
the real one) and assert the failure modes a results table must not have -- a ``0 of 0``
read as a pass, an interval on no data, a verdict outside its pre-registration reported
as fine, an E7 disagreement dropped, and evidence that changed after it was hashed.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from replay.fidelity import _report
from replay.fidelity._harness import (
    REPLAY_BUDGET_S,
    SCENARIOS,
    PhaseObservation,
    Scenario,
    Score,
    classify,
)

# ── classify: a verdict needs something to have been measured ────────────────


def _obs(**kw: Any) -> PhaseObservation:
    base: dict[str, Any] = {
        "started": True,
        "finished": True,
        "exit_code": 0,
        "output_digest": "d" * 64,
        "output": {},
        "model_hits": 0,
        "canary_in_output": False,
        "tool_bodies": 0,
        "connections": 0,
        "outside_writes": [],
        "wall_s": 0.1,
    }
    return PhaseObservation(**{**base, **kw})


_REPORT_OK = {"status": "success", "divergences": []}


def test_a_workload_that_exercised_nothing_is_not_scored_faithful() -> None:
    """``0 of 0 served`` is no evidence: the verdict is ``unmeasured``, never ``faithful``."""
    verdict, outcome_equal, _ = classify(_obs(), _obs(), _REPORT_OK, claims=())
    assert outcome_equal  # the outcome did reproduce -- there was just nothing to reproduce
    assert verdict == "unmeasured"


def test_a_claimed_surface_the_capture_never_exercised_is_not_faithful() -> None:
    """A scenario that claims a model call, whose capture oracle saw none, measured nothing."""
    verdict, _, _ = classify(_obs(), _obs(), _REPORT_OK, claims=("model",))
    assert verdict == "unmeasured"


def test_an_exercised_claim_still_scores_faithful() -> None:
    """The non-vacuity rule must not turn every verdict into ``unmeasured``."""
    verdict, _, _ = classify(_obs(model_hits=2), _obs(model_hits=0), _REPORT_OK, claims=("model",))
    assert verdict == "faithful"


def test_unmeasured_never_hides_a_live_call() -> None:
    """Live activity outranks the non-vacuity rule: it is reported as live, not unmeasured."""
    verdict, _, _ = classify(
        _obs(), _obs(model_hits=1, canary_in_output=True), _REPORT_OK, claims=()
    )
    assert verdict in ("live-reported", "live-unreported")


def test_every_registered_scenario_claims_something_it_must_exercise() -> None:
    for sid, scenario in SCENARIOS.items():
        assert scenario.claims, f"{sid} claims no surface, so no verdict on it could be measured"
        assert scenario.surface, f"{sid} has no surface identity for the per-surface report"


def test_scenario_claims_come_from_steps_and_an_unknown_step_is_an_error() -> None:
    assert Scenario("X", "t", ("model", "net")).claims == ("model", "network")
    with pytest.raises(KeyError):
        _ = Scenario("X", "t", ("not-a-step",)).claims


def test_a_replay_over_budget_is_not_faithful() -> None:
    """E6: the budget is part of the verdict, not a log line."""
    slow = _obs(wall_s=REPLAY_BUDGET_S + 1, model_hits=0)
    verdict, outcome_equal, _ = classify(_obs(model_hits=2), slow, _REPORT_OK, claims=("model",))
    assert outcome_equal
    assert verdict == "diverged"
    on_time = _obs(wall_s=REPLAY_BUDGET_S)
    assert classify(_obs(model_hits=2), on_time, _REPORT_OK, claims=("model",))[0] == "faithful"


# ── Wilson interval ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        (5, 5, 0.5655, 1.0),
        (0, 5, 0.0, 0.4345),
        (1, 1, 0.2065, 1.0),
        (10, 10, 0.7225, 1.0),
        (28, 28, 0.8795, 1.0),
        (3, 10, 0.1078, 0.6032),
    ],
)
def test_wilson_matches_published_values(k: int, n: int, lo: float, hi: float) -> None:
    got = _report.wilson(k, n)
    assert got is not None
    assert got[0] == pytest.approx(lo, abs=5e-4)
    assert got[1] == pytest.approx(hi, abs=5e-4)


def test_wilson_on_no_data_is_none_not_a_perfect_interval() -> None:
    assert _report.wilson(0, 0) is None


@pytest.mark.parametrize("bad", [(-1, 3), (4, 3), (1, -2)])
def test_wilson_rejects_impossible_counts(bad: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="impossible"):
        _report.wilson(*bad)


def test_wilson_always_brackets_the_point_estimate_inside_zero_one() -> None:
    for n in range(1, 40):
        for k in range(n + 1):
            interval = _report.wilson(k, n)
            assert interval is not None
            lo, hi = interval
            assert 0.0 <= lo <= k / n <= hi <= 1.0, (k, n, lo, hi)


# ── building a report from retained baselines ────────────────────────────────


def _summary(sid: str, *, verdict: str | None = None, **replay_kw: Any) -> dict[str, Any]:
    """A ``Score.summary()`` for *sid* as the harness would write it, round-tripped via JSON."""
    scenario = SCENARIOS[sid]
    cap = _obs(
        model_hits=2 if "model" in scenario.claims else 0,
        tool_bodies=1 if "tool" in scenario.claims else 0,
        connections=1 if "network" in scenario.claims else 0,
        outside_writes=["ledger.txt"] if "side_effects" in scenario.claims else [],
    )
    rep = _obs(**replay_kw)
    got, equal, ack = classify(cap, rep, _REPORT_OK, claims=scenario.claims)
    score = Score(
        sid=sid,
        capture=cap,
        replay=rep,
        report={
            "status": "success",
            "model_calls_live": 0,
            "tool_calls_live": 0,
            "network_connections_live": 0,
            "child_processes_spawned": 0,
            "fidelity_verdict": "faithful",
            "divergences": [],
        },
        e7={
            kind: {"oracle": n, "reported": 0, "agree": n == 0, "acknowledged": ack[kind]}
            for kind, n in rep.live.items()
        },
        acknowledged=ack,
        outcome_equal=equal,
        verdict=verdict or got,
        expected=sorted(scenario.expected),
    )
    out = json.loads(json.dumps({"title": scenario.title, **score.summary()}))
    return out  # type: ignore[no-any-return]


def _good_provenance(**over: Any) -> dict[str, Any]:
    """Provenance as ``_report.provenance()`` writes it for a clean, pinned session."""
    base: dict[str, Any] = {
        "novafabric": "0.0.0",
        "git_sha": "0" * 40,
        "git_dirty": False,
        "python": "3.12.3",
        "implementation": "CPython",
        "platform": "Linux-test",
        "machine": "x86_64",
        "dependencies": {"openai": "1", "httpx": "1", "pytest": "1", "pyyaml": "1"},
        "fixture_sha256": {
            "_harness.py": "a" * 64,
            "_workload.py": "b" * 64,
            "conftest.py": "c" * 64,
        },
        "replay_budget_s": 60.0,
    }
    return {**base, **over}


def _control_entry(name: str) -> dict[str, Any]:
    """A control record whose verdict is one its pre-registration accepts."""
    if name == "report-only:S6b":
        return {"oracle": "live-unreported", "report_only": "faithful"}
    verdict = sorted(_report.REQUIRED_CONTROLS[name])[0]
    return {"verdict": verdict, "live": None, "outcome_equal": False, "report": None}


def _good_controls() -> dict[str, Any]:
    return {name: _control_entry(name) for name in _report.REQUIRED_CONTROLS}


def _baseline(*summaries: dict[str, Any], **over: Any) -> dict[str, Any]:
    """A complete, evidence-grade baseline; keyword arguments replace whole top-level keys."""
    base: dict[str, Any] = {
        "issue": "#59",
        "scenarios": {s["sid"]: s for s in summaries},
        "controls": _good_controls(),
        "provenance": _good_provenance(),
    }
    return {**base, **over}


def test_a_surface_with_only_unmeasured_runs_has_no_interval_and_is_not_ok() -> None:
    empty = _summary("S1", verdict="unmeasured")
    report = _report.build([_baseline(empty)])
    row = report["surfaces"][SCENARIOS["S1"].surface]
    assert row["measured"] == 0 and row["unmeasured"] == 1
    assert row["wilson95"] is None
    assert row["status"] == "UNMEASURED"
    assert report["ok"] is False


def test_unmeasured_runs_are_excluded_from_the_denominator() -> None:
    good = _summary("S1")
    vacuous = _summary("S1", verdict="unmeasured")
    row = _report.build([_baseline(good), _baseline(vacuous)])["surfaces"][SCENARIOS["S1"].surface]
    assert (row["attempted"], row["measured"], row["unmeasured"]) == (2, 1, 1)
    assert row["met_preregistration"] == 1
    assert row["wilson95"] == list(_report.wilson(1, 1) or ())


def test_a_vacuous_scenario_fails_the_report_even_when_its_surface_has_good_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scenario that measured nothing is a harness defect: it must not hide in a good surface.

    Two scenarios share one surface, each consistent across runs, so only the
    ``unmeasured`` count (not the run-to-run consistency check) can fail the report.
    """
    shared = dataclasses.replace(SCENARIOS["S10"], surface=SCENARIOS["S1"].surface)
    monkeypatch.setitem(SCENARIOS, "S10", shared)
    report = _report.build([_baseline(_summary("S1"), _summary("S10", verdict="unmeasured"))])
    row = report["surfaces"][SCENARIOS["S1"].surface]
    assert (row["status"], row["measured"], row["unmeasured"]) == ("OK", 1, 1)
    assert report["inconsistent_across_runs"] == []
    assert report["ok"] is False


def test_a_verdict_outside_the_preregistration_is_unexpected_and_fails_the_report() -> None:
    bad = _summary("S1", verdict="diverged")
    report = _report.build([_baseline(bad)])
    row = report["surfaces"][SCENARIOS["S1"].surface]
    assert row["status"] == "UNEXPECTED"
    assert row["unexpected"] == 1
    assert report["ok"] is False


def test_a_clean_run_of_a_served_surface_is_ok() -> None:
    report = _report.build([_baseline(_summary("S1")), _baseline(_summary("S1"))])
    row = report["surfaces"][SCENARIOS["S1"].surface]
    assert row["status"] == "OK"
    assert (row["faithful"], row["measured"]) == (2, 2)
    assert report["ok"] is True


def test_verdicts_that_differ_between_runs_are_flagged() -> None:
    a = _summary("S1")
    b = _summary("S1", verdict="refused")
    report = _report.build([_baseline(a), _baseline(b)])
    assert report["surfaces"][SCENARIOS["S1"].surface]["verdicts"] == {"faithful": 1, "refused": 1}
    assert "S1" in report["inconsistent_across_runs"]


def test_an_unknown_scenario_is_rejected_not_silently_dropped() -> None:
    odd = _summary("S1")
    odd["sid"] = "S99"
    with pytest.raises(ValueError, match="S99"):
        _report.build([_baseline(odd)])


def test_an_e7_counter_disagreement_is_listed_even_when_acknowledged() -> None:
    """S5's shape: the oracle saw a connection, the counter says 0, a child count admits it."""
    s5 = _summary("S5", connections=1)
    s5["e7"]["network"] = {"oracle": 1, "reported": 0, "agree": False, "acknowledged": True}
    report = _report.build([_baseline(s5)])
    (found,) = [d for d in report["e7_disagreements"] if (d["sid"], d["kind"]) == ("S5", "network")]
    assert (found["oracle"], found["reported"], found["acknowledged"]) == (1, 0, True)


@pytest.mark.parametrize(
    ("coverage", "disclosed"), [("partial", True), ("complete", False), (None, False)]
)
def test_a_network_miss_is_a_disclosed_limitation_only_if_the_engine_said_it_was_blind(
    coverage: str | None, disclosed: bool
) -> None:
    """An undeclared miss is a defect; a declared one is a documented limit of the surface."""
    s5 = _summary("S5", connections=1)
    s5["e7"]["network"] = {"oracle": 1, "reported": 0, "agree": False, "acknowledged": True}
    s5["report"]["network_observation_coverage"] = coverage
    report = _report.build([_baseline(s5)])
    (found,) = [d for d in report["e7_disagreements"] if (d["sid"], d["kind"]) == ("S5", "network")]
    assert found["declared_limitation"] is disclosed


def test_a_report_that_says_faithful_where_the_oracle_does_not_is_an_e7_disagreement() -> None:
    s6 = _summary("S6", outside_writes=["ledger.txt"])  # oracle: live side effect
    s6["report"]["fidelity_verdict"] = "faithful"
    report = _report.build([_baseline(s6)])
    kinds = {d["kind"] for d in report["e7_disagreements"] if d["sid"] == "S6"}
    assert "verdict" in kinds


def _entry(report: dict[str, Any], kind: str) -> dict[str, int]:
    return report["e7_confusion"][kind]


def test_e7_confusion_matrix_counts_oracle_against_counter_per_kind() -> None:
    """tp: both saw it; fn: oracle saw it, the counter says 0 (the dangerous cell)."""
    seen = _summary("S4", connections=1)  # oracle 1, counter 1
    seen["e7"]["network"] = {"oracle": 1, "reported": 1, "agree": True, "acknowledged": True}
    missed = _summary("S5", connections=1)  # oracle 1, counter 0
    missed["e7"]["network"] = {"oracle": 1, "reported": 0, "agree": False, "acknowledged": True}
    quiet = _summary("S1")  # nothing live anywhere
    report = _report.build([_baseline(seen, missed, quiet)])
    net = _entry(report, "network")
    assert (net["tp"], net["fn"], net["fp"], net["tn"]) == (1, 1, 0, 1)
    assert net["no_counter"] == 0


def test_e7_confusion_separates_a_missing_counter_from_a_zero_counter() -> None:
    """Side effects have no counter in replay_result.yaml: that is neither a hit nor a miss."""
    written = _summary("S6", outside_writes=["ledger.txt"])
    written["e7"]["side_effects"] = {
        "oracle": 1,
        "reported": None,
        "agree": None,
        "acknowledged": True,
    }
    side = _entry(_report.build([_baseline(written)]), "side_effects")
    assert (side["tp"], side["fn"], side["fp"]) == (0, 0, 0)
    assert side["no_counter"] == 1 and side["no_counter_oracle_positive"] == 1


def test_e7_false_positive_is_a_counter_above_zero_with_no_oracle_activity() -> None:
    odd = _summary("S1")
    odd["e7"]["model"] = {"oracle": 0, "reported": 3, "agree": False, "acknowledged": True}
    assert _entry(_report.build([_baseline(odd)]), "model")["fp"] == 1


def test_replay_wall_time_is_summarised_over_executed_runs_only() -> None:
    fast, slow = _summary("S1", wall_s=1.0), _summary("S1", wall_s=3.0)
    refused = _summary("S1", started=False, finished=False, wall_s=0.5)
    row = _report.build([_baseline(fast), _baseline(slow), _baseline(refused)])["surfaces"][
        SCENARIOS["S1"].surface
    ]
    assert row["replay_wall_s"] == {"n": 2, "mean": 2.0, "max": 3.0}


def _control(verdict: str, live: dict[str, int], **counters: int) -> dict[str, Any]:
    report = {"model_calls_live": 0, "tool_calls_live": 0, "network_connections_live": 0}
    return {
        "verdict": verdict,
        "live": live,
        "outcome_equal": False,
        "report": {**report, **counters},
    }


def test_controls_have_their_own_e7_matrix_so_a_missed_live_call_cannot_hide() -> None:
    """A perturbation the oracle sees but the counter misses is an fn, listed apart from the corpus."""
    zero = {"model": 0, "tool": 0, "network": 0, "side_effects": 0}
    base = _baseline(_summary("S1"))
    base["controls"] = {
        "force-live": _control("live-unreported", {**zero, "model": 1}),
        "tool-not-served": _control("live-reported", {**zero, "tool": 1}, tool_calls_live=1),
        "drop-response": _control("refused", zero),
    }
    report = _report.build([base])
    matrix = report["e7_confusion_controls"]
    assert (matrix["model"]["fn"], matrix["model"]["tp"], matrix["model"]["tn"]) == (1, 0, 2)
    assert (matrix["tool"]["tp"], matrix["tool"]["fn"]) == (1, 0)
    # ...and the corpus matrix is not polluted by perturbation controls
    assert report["e7_confusion"]["model"]["fn"] == 0


def test_a_control_without_oracle_data_is_left_out_of_the_control_matrix() -> None:
    base = _baseline(_summary("S1"))
    base["controls"] = {"report-only:S6b": {"oracle": "live-unreported", "report_only": "faithful"}}
    assert _report.build([base])["e7_confusion_controls"] == {}


def test_the_report_states_the_scope_of_its_oracles() -> None:
    report = _report.build([_baseline(_summary("S1"))])
    scope = " ".join(report["oracle_scope"]).lower()
    assert "loopback" in scope and "not exhaustive" in scope
    assert "synthetic" in scope
    assert report["provenance"]["git_sha"] == "0" * 40


def test_a_baseline_without_provenance_is_reported_as_such() -> None:
    base = _baseline(_summary("S1"))
    del base["provenance"]
    report = _report.build([base])
    assert report["provenance"] is None
    assert report["ok"] is False


# ── retained evidence: hashed, and verifiable after the fact ─────────────────


def test_evidence_manifest_hashes_files_and_detects_a_change(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "S1").mkdir(parents=True)
    (root / "S1" / "markers.capture").write_text("start capture\nend capture rc=0\n")
    (root / "S1" / "model-hits.capture").write_text("capture openai /v1/chat/completions q1\n")
    (root / "S1" / "home").mkdir()
    (root / "S1" / "home" / "registry.db").write_text("must not be copied")
    dest = tmp_path / "evidence"
    manifest = _report.collect_evidence(root, dest)
    assert sorted(manifest) == ["S1/markers.capture", "S1/model-hits.capture"]
    assert _report.verify_evidence(dest, manifest) == []
    (dest / "S1" / "markers.capture").write_text("end capture rc=0\n")
    assert _report.verify_evidence(dest, manifest) == ["S1/markers.capture"]
    (dest / "S1" / "model-hits.capture").unlink()
    assert _report.verify_evidence(dest, manifest) == [
        "S1/markers.capture",
        "S1/model-hits.capture",
    ]


def test_evidence_collection_skips_anything_that_could_hold_credentials(tmp_path: Path) -> None:
    root = tmp_path / "src"
    run = root / "S1" / "runs" / "01RUN"
    run.mkdir(parents=True)
    for name in ("model-calls.jsonl", "tool-calls.jsonl", "replayability.json", ".env", "cred.pem"):
        (run / name).write_text("{}\n")
    (root / "S1" / "replays" / "01R").mkdir(parents=True)
    (root / "S1" / "replays" / "01R" / "replay_result.yaml").write_text("status: success\n")
    manifest = _report.collect_evidence(root, tmp_path / "evidence")
    assert sorted(manifest) == [
        "S1/replays/01R/replay_result.yaml",
        "S1/runs/01RUN/model-calls.jsonl",
        "S1/runs/01RUN/replayability.json",
        "S1/runs/01RUN/tool-calls.jsonl",
    ]


def test_render_markdown_never_prints_a_rate_for_an_unmeasured_surface() -> None:
    empty = _summary("S1", verdict="unmeasured")
    text = _report.render_markdown(_report.build([_baseline(empty)]))
    row = next(line for line in text.splitlines() if SCENARIOS["S1"].surface in line)
    assert "UNMEASURED" in row
    assert "100%" not in row and "0/0" not in row


# ── evidence-grade: controls, provenance, vacuous verdicts (PR review blockers) ─────────


def _ok_report(**over: Any) -> dict[str, Any]:
    return _report.build([_baseline(_summary("S1"), **over)])


def test_a_complete_evidence_grade_baseline_is_ok() -> None:
    report = _ok_report()
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["controls_ok"] is True


def test_missing_required_controls_fail_the_report_even_with_perfect_scenarios() -> None:
    report = _ok_report(controls={})
    assert report["surfaces"][SCENARIOS["S1"].surface]["status"] == "OK"
    assert report["ok"] is False
    assert set(report["required_controls_missing"]) == set(_report.REQUIRED_CONTROLS)


def test_one_control_missing_in_one_session_fails_the_report() -> None:
    second = _baseline(_summary("S1"))
    del second["controls"]["force-live"]
    report = _report.build([_baseline(_summary("S1")), second])
    assert report["controls"]["force-live"]["status"] == "MISSING"
    assert report["controls"]["force-live"]["missing_runs"] == 1
    assert report["ok"] is False


@pytest.mark.parametrize("name", sorted(_report.REQUIRED_CONTROLS))
def test_every_required_control_that_comes_back_faithful_fails_the_report(name: str) -> None:
    controls = _good_controls()
    controls[name] = (
        {"oracle": "faithful", "report_only": "faithful"}
        if name == "report-only:S6b"
        else {**controls[name], "verdict": "faithful"}
    )
    report = _ok_report(controls=controls)
    assert report["controls"][name]["status"] == "FAILED"
    assert report["ok"] is False


def test_a_control_verdict_outside_its_own_preregistration_fails_even_if_not_faithful() -> None:
    controls = _good_controls()
    controls["mutated-output"] = {**controls["mutated-output"], "verdict": "refused"}
    report = _ok_report(controls=controls)
    assert report["controls"]["mutated-output"]["status"] == "FAILED"
    assert report["ok"] is False


def test_the_calls_nothing_control_must_stay_unmeasured() -> None:
    controls = _good_controls()
    controls["calls-nothing"] = {**controls["calls-nothing"], "verdict": "faithful"}
    assert _ok_report(controls=controls)["controls"]["calls-nothing"]["status"] == "FAILED"


def test_the_report_only_control_must_actually_flip_the_verdict() -> None:
    controls = _good_controls()
    controls["report-only:S6b"] = {"oracle": "live-unreported", "report_only": "live-unreported"}
    report = _ok_report(controls=controls)
    assert report["controls"]["report-only:S6b"]["status"] == "FAILED"
    assert report["ok"] is False


def test_an_unlisted_extra_control_that_scores_faithful_still_fails() -> None:
    controls = {**_good_controls(), "surprise": {"verdict": "faithful"}}
    report = _ok_report(controls=controls)
    assert report["controls"]["surprise"]["status"] == "FAILED"
    assert report["ok"] is False


def test_required_controls_are_exactly_the_ones_the_controls_module_records() -> None:
    """Drift guard: a control added to (or dropped from) the corpus must change the required set."""
    import re

    source = (Path(__file__).parent / "test_fidelity_controls.py").read_text()
    recorded = set(re.findall(r'_record\(corpus, "([^"{]+)"', source))
    shaped = set(re.findall(r'_record\(corpus, f"([^"{]+):\{sid\}"', source))
    expected = recorded | {f"{name}:{sid}" for name in shaped for sid in ("S10", "S11", "S12")}
    expected.add("report-only:S6b")  # recorded through a dict literal, not _record()
    assert set(_report.REQUIRED_CONTROLS) == expected


def test_a_faithful_verdict_the_capture_oracles_cannot_support_is_rederived_unmeasured() -> None:
    """An old or edited baseline may say faithful with nothing exercised; the report decides."""
    summary = _summary("S1")
    summary["capture"]["model_hits"] = 0  # the capture oracle saw no model call
    assert summary["verdict"] == "faithful"
    report = _ok_report_from(summary)
    row = report["surfaces"][SCENARIOS["S1"].surface]
    assert (row["measured"], row["unmeasured"], row["faithful"]) == (0, 1, 0)
    assert row["status"] == "UNMEASURED"
    assert report["ok"] is False


def _ok_report_from(*summaries: dict[str, Any]) -> dict[str, Any]:
    return _report.build([_baseline(*summaries)])


def test_zero_observed_tool_calls_do_not_prove_tool_replay() -> None:
    summary = _summary("S3")
    summary["capture"]["tool_bodies"] = 0
    row = _ok_report_from(summary)["surfaces"][SCENARIOS["S3"].surface]
    assert (row["measured"], row["faithful"]) == (0, 0)


# provenance


@pytest.mark.parametrize(
    "bad",
    [
        {"git_sha": "abc"},
        {"git_sha": "Z" * 40},
        {"git_sha": None},
        {"git_dirty": True},
        {"git_dirty": None},
        {"novafabric": None},
        {"python": ""},
        {"fixture_sha256": {}},
        {"fixture_sha256": {"_harness.py": "short"}},
        {"dependencies": {}},
    ],
)
def test_invalid_or_unclean_provenance_is_never_evidence_grade(bad: dict[str, Any]) -> None:
    report = _ok_report(provenance=_good_provenance(**bad))
    assert report["ok"] is False
    assert any("provenance" in p for p in report["problems"])


def test_missing_provenance_is_never_evidence_grade() -> None:
    base = _baseline(_summary("S1"))
    del base["provenance"]
    report = _report.build([base])
    assert report["ok"] is False and report["provenance"] is None
    assert any("provenance" in p for p in report["problems"])


@pytest.mark.parametrize(
    "change",
    [
        {"git_sha": "1" * 40},
        {"python": "3.13.0"},
        {"platform": "Darwin-other"},
        {"novafabric": "9.9.9"},
        {"dependencies": {"openai": "2", "httpx": "1", "pytest": "1", "pyyaml": "1"}},
        {
            "fixture_sha256": {
                "_harness.py": "d" * 64,
                "_workload.py": "b" * 64,
                "conftest.py": "c" * 64,
            }
        },
    ],
)
def test_sessions_with_different_provenance_are_never_silently_combined(
    change: dict[str, Any],
) -> None:
    a = _baseline(_summary("S1"))
    b = _baseline(_summary("S1"), provenance=_good_provenance(**change))
    with pytest.raises(_report.MixedProvenanceError, match="provenance"):
        _report.build([a, b])


def test_a_session_without_provenance_cannot_be_mixed_with_one_that_has_it() -> None:
    a = _baseline(_summary("S1"))
    b = _baseline(_summary("S1"))
    del b["provenance"]
    with pytest.raises(_report.MixedProvenanceError):
        _report.build([a, b])


def test_mixed_baselines_can_be_explicitly_partitioned_into_independent_reports() -> None:
    a1, a2 = _baseline(_summary("S1")), _baseline(_summary("S1"))
    b = _baseline(_summary("S1"), provenance=_good_provenance(git_sha="2" * 40))
    groups = _report.partition_by_provenance([a1, b, a2])
    assert [len(g) for g in groups] == [2, 1]
    assert all(_report.build(g)["ok"] for g in groups)


def test_volatile_provenance_fields_do_not_split_identical_sessions() -> None:
    """Two sessions of one commit must combine: only the pinned fields are compared."""
    a = _baseline(_summary("S1"))
    b = _baseline(_summary("S1"), provenance=_good_provenance())
    assert _report.build([a, b])["ok"] is True


# headline: what "13 surfaces" does and does not mean


def test_headline_separates_faithful_refused_and_live_reported_surfaces() -> None:
    report = _ok_report_from(
        _summary("S1"), _summary("S2", verdict="refused"), _summary("S4", verdict="live-reported")
    )
    h = report["headline"]
    assert h["surfaces"] == 3 and h["met_preregistration"] == 3
    assert (h["faithful"], h["correctly_refused"], h["live_reported"]) == (1, 1, 1)
    assert h["faithful"] < h["met_preregistration"]


def test_markdown_states_that_met_preregistration_is_not_all_faithful() -> None:
    text = _report.render_markdown(
        _ok_report_from(_summary("S1"), _summary("S2", verdict="refused"))
    )
    assert "not" in text.split("## Scenarios")[0].lower()
    assert "faithful replay" in text.split("## Scenarios")[0].lower()


def test_markdown_lists_every_reason_a_report_is_not_ok() -> None:
    text = _report.render_markdown(_ok_report(controls={}))
    assert "NOT OK" in text and "force-live" in text


def test_the_zero_recorded_calls_caveat_is_in_every_report() -> None:
    scope = " ".join(_report.build([_baseline(_summary("S1"))])["oracle_scope"]).lower()
    assert "recorded no" in scope and "vacuous" in scope


# command line: exit codes and partitioning


def _write(path: Path, baseline: dict[str, Any]) -> Path:
    path.write_text(json.dumps(baseline))
    return path


def test_cli_exits_zero_only_for_an_evidence_grade_ok_report(tmp_path: Path) -> None:
    ok = _write(tmp_path / "a.json", _baseline(_summary("S1")))
    assert _report.main([str(ok)]) == 0
    bad = _write(tmp_path / "b.json", _baseline(_summary("S1"), controls={}))
    assert _report.main([str(bad)]) == 1


def test_cli_rejects_mixed_provenance_and_writes_nothing(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.json", _baseline(_summary("S1")))
    b = _write(
        tmp_path / "b.json",
        _baseline(_summary("S1"), provenance=_good_provenance(git_sha="3" * 40)),
    )
    out = tmp_path / "report.json"
    assert _report.main([str(a), str(b), "--json", str(out)]) == 2
    assert not out.exists()


def test_cli_partition_writes_one_independent_report_per_provenance(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.json", _baseline(_summary("S1")))
    b = _write(
        tmp_path / "b.json",
        _baseline(_summary("S1"), provenance=_good_provenance(git_sha="3" * 40)),
    )
    out = tmp_path / "report.json"
    assert _report.main([str(a), str(b), "--partition", "--json", str(out)]) == 0
    parts = sorted(tmp_path.glob("report.part*.json"))
    assert [p.name for p in parts] == ["report.part1.json", "report.part2.json"]
    shas = {json.loads(p.read_text())["provenance"]["git_sha"] for p in parts}
    assert shas == {"0" * 40, "3" * 40}
    assert not out.exists()


def test_cli_rejects_a_file_that_is_not_a_baseline(tmp_path: Path) -> None:
    bogus = _write(tmp_path / "x.json", {"hello": "world"})  # type: ignore[arg-type]
    assert _report.main([str(bogus)]) == 2


def test_a_scenario_that_claims_nothing_can_never_be_faithful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if the registry ever held one, ``all([])`` must not read as exercised."""
    empty = dataclasses.replace(SCENARIOS["S1"], steps=())
    monkeypatch.setitem(SCENARIOS, "S1", empty)
    assert empty.claims == ()
    assert (
        _report.effective_verdict(
            "S1", {"verdict": "faithful", "capture": _summary("S1")["capture"]}
        )
        == "unmeasured"
    )


def test_every_classify_call_in_the_harness_passes_claims() -> None:
    """``claims=None`` skips the non-vacuity guard, so no caller may omit it.

    A source-level guard rather than a signature change: the measuring files (_harness.py,
    _workload.py, conftest.py) stay byte-identical to the ones the retained sessions ran.
    """
    import ast

    tree = ast.parse((Path(__file__).parent / "_harness.py").read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "classify"
    ]
    assert calls, "the harness no longer calls classify()"
    assert all(any(k.arg == "claims" for k in c.keywords) for c in calls)


def test_surface_results_never_aggregate_the_descriptive_e_flags() -> None:
    """E1-E6 ``pass`` flags are per-run observations: a surface's status comes from verdicts only."""
    summary = _summary("S1")
    base = _report.build([_baseline(summary)])
    flipped = _summary("S1")
    # an E4-style flag changing (writes appear outside the work dir) must not move the status
    flipped["replay"]["outside_writes"] = ["x"]
    flipped["verdict"] = "faithful"
    again = _report.build([_baseline(flipped)])
    assert base["surfaces"][SCENARIOS["S1"].surface]["status"] == "OK"
    assert again["surfaces"][SCENARIOS["S1"].surface]["status"] == "OK"
