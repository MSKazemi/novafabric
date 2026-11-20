"""Replay fidelity corpus S1-S8, scored by independent oracles (issue #59).

Each scenario carries a pre-registered set of acceptable verdicts
(``_harness.SCENARIOS``). Where current code does not meet it, the test encodes the
true expected behaviour and is ``xfail(strict=True)`` against the issue that fixes it,
so the fix shows up as an XPASS failure that must be un-marked.

Plain tests here also pin what the *oracles* observe today, independently of the
report -- that is what makes an E7 (report truthfulness) disagreement a finding rather
than an artefact of the harness.
"""

from __future__ import annotations

import pytest

# The workload needs the optional `openai` extra; httpx drives every fake transport.
pytest.importorskip("openai")
pytest.importorskip("httpx")

from replay.fidelity._harness import SCENARIOS, Corpus  # noqa: E402

#: One xdist group: the session-scoped corpus (conftest.py) is then built once per
#: worker instead of once per worker that happens to receive a fidelity test.
pytestmark = [pytest.mark.timeout(300), pytest.mark.xdist_group("replay-fidelity")]

#: Scenarios whose pre-registered expectation current code does not meet (none today;
#: S6 was here until the ``file_effects`` check, ADR-0321 amendment).
_XFAIL: dict[str, pytest.MarkDecorator] = {}


@pytest.mark.parametrize("sid", [pytest.param(sid, marks=_XFAIL.get(sid, ())) for sid in SCENARIOS])
def test_verdict_matches_the_preregistered_expectation(corpus: Corpus, sid: str) -> None:
    score = corpus.score(sid)
    assert score.verdict in SCENARIOS[sid].expected, (
        f"{sid}: verdict {score.verdict!r}, expected one of {sorted(SCENARIOS[sid].expected)}; "
        f"live={score.replay.live} e7={score.e7} report={score.report}"
    )


# ── what the oracles see today (all pass on current code) ────────────────────


@pytest.mark.parametrize("sid", list(SCENARIOS))
def test_every_capture_ran_and_reached_its_own_oracles(corpus: Corpus, sid: str) -> None:
    cap = corpus.captured(sid).observation
    assert cap.started and cap.finished
    assert cap.exit_code == (1 if "exit1" in SCENARIOS[sid].steps else 0)
    assert not cap.canary_in_output
    if "model" in SCENARIOS[sid].steps:
        assert cap.model_hits == 2  # the capture really called the fake transport
    if "tool" in SCENARIOS[sid].steps:
        assert cap.tool_bodies == 1
    if {"net", "child_net"} & set(SCENARIOS[sid].steps):
        assert cap.connections == 1
    if "ollama" in SCENARIOS[sid].steps:
        assert cap.model_hits == cap.connections == 1


@pytest.mark.parametrize("sid", ["S1", "S3", "S7", "S8"])
def test_served_scenarios_make_no_live_call_and_reproduce_the_outcome(
    corpus: Corpus, sid: str
) -> None:
    score = corpus.score(sid)
    assert score.replay.live == {"model": 0, "tool": 0, "network": 0, "side_effects": 0}
    assert score.outcome_equal
    assert score.verdict == "faithful"


def test_s2_default_level_tool_is_refused_before_its_body_runs(corpus: Corpus) -> None:
    score = corpus.score("S2")
    assert score.replay.tool_bodies == 0
    assert not score.replay.finished
    assert score.report["status"] != "success"
    assert "tool_result_not_servable" in score.report["divergences"]


def test_s4_python_socket_is_seen_by_the_oracle_and_the_report(corpus: Corpus) -> None:
    score = corpus.score("S4")
    assert score.replay.connections == 1
    assert score.e7["network"] == {
        "oracle": 1,
        "reported": 1,
        "agree": True,
        "acknowledged": True,
    }


def test_s5_oracle_sees_the_child_process_connection(corpus: Corpus) -> None:
    score = corpus.score("S5")
    assert score.replay.output == score.capture.output  # bash connected both times
    assert score.replay.connections == 1


def test_s6_oracle_sees_the_side_effect_repeated(corpus: Corpus) -> None:
    score = corpus.score("S6")
    assert score.capture.outside_writes == score.replay.outside_writes == ["ledger.txt"]


def test_s6_report_names_the_repeated_file(corpus: Corpus) -> None:
    """The verdict is diverged *because of* the file: the ``file_effects`` check."""
    score = corpus.score("S6")
    assert score.report["fidelity_verdict"] == "diverged", score.report
    assert score.report["fidelity_failing"] == ["file_effects"], score.report


def test_s9_unserved_ollama_call_is_refused_before_anything_runs(corpus: Corpus) -> None:
    """#49 (ADR-0319): a recorded model call replay cannot serve makes strict replay refuse.

    Baseline 2026-11-19 (then S7, Ollama /api/chat before ADR-0320 served it): the oracle
    saw one live hit with the canary in the output while the report said 0 live model calls.
    """
    score = corpus.score("S9")
    assert score.capture.model_hits == 1  # the capture really called the fake Ollama
    assert not score.replay.started
    assert score.replay.model_hits == 0
    assert score.verdict == "refused"


# ── E7 report truthfulness: the true behaviour, xfail until fixed ────────────


def test_report_does_not_claim_zero_live_model_calls_when_the_oracle_saw_one(
    corpus: Corpus,
) -> None:
    """#49: either nothing ran live (refused), or the report admits the live call."""
    score = corpus.score("S9")
    assert score.replay.live["model"] == 0 or score.acknowledged["model"], score.report
    assert score.verdict in ("refused", "live-reported")


def test_report_acknowledges_a_child_process_connection(corpus: Corpus) -> None:
    score = corpus.score("S5")
    assert score.replay.live["network"] >= 1
    assert score.acknowledged["network"], score.report


def test_report_does_not_call_a_repeated_side_effect_faithful(corpus: Corpus) -> None:
    score = corpus.score("S6")
    assert score.replay.live["side_effects"] >= 1
    assert score.acknowledged["side_effects"], score.report


def test_faithful_replay_of_a_failed_run_is_reported_faithful(corpus: Corpus) -> None:
    score = corpus.score("S8")
    assert score.verdict == "faithful"  # the harness's verdict, holds today
    assert score.report["fidelity_verdict"] == "faithful", score.report
