"""Controls: the fidelity harness can fail (issue #59).

Each control perturbs one thing the harness claims to detect and asserts that the
HARNESS -- not the report -- returns the failing score. The unperturbed counterpart
(S1/S3 scored ``faithful``) is in ``test_fidelity_corpus.py``; together they are the
red-green pair.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The workload needs the optional `openai` extra; httpx drives every fake transport.
pytest.importorskip("openai")
pytest.importorskip("httpx")

from replay.fidelity._harness import CANARY, Corpus, Score, score_from_report_only  # noqa: E402

#: One xdist group: the session-scoped corpus (conftest.py) is then built once per
#: worker instead of once per worker that happens to receive a fidelity test.
pytestmark = [pytest.mark.timeout(300), pytest.mark.xdist_group("replay-fidelity")]


def _record(corpus: Corpus, name: str, score: Score) -> None:
    corpus.controls[name] = {
        "verdict": score.verdict,
        "live": score.replay.live,
        "outcome_equal": score.outcome_equal,
        "report": score.report,
    }


def test_deleting_one_recorded_model_response_is_not_scored_faithful(corpus: Corpus) -> None:
    def drop_second_model_call(capsule: Path) -> None:
        path = capsule / "model-calls.jsonl"
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        assert len(lines) == 2
        path.write_text(lines[0] + "\n")

    score = corpus.harness.replay(
        corpus.captured("S1"), mutate_capsule=drop_second_model_call, name="drop-response"
    )
    _record(corpus, "drop-response", score)
    assert score.verdict != "faithful", score
    assert not score.outcome_equal


def test_a_live_canary_call_is_scored_not_faithful(corpus: Corpus) -> None:
    score = corpus.harness.replay(
        corpus.captured("S1"), extra={"FIDELITY_FORCE_LIVE_MODEL": "1"}, name="force-live"
    )
    _record(corpus, "force-live", score)
    assert score.replay.model_hits >= 1
    assert score.replay.canary_in_output
    assert score.replay.output is not None and f"{CANARY}:q1" in score.replay.output["model"]
    assert score.verdict in ("live-reported", "live-unreported")
    assert score.verdict != "faithful"


def test_an_unserved_tool_is_scored_live_by_the_sentinel(corpus: Corpus) -> None:
    def drop_tool_records(capsule: Path) -> None:
        (capsule / "tool-calls.jsonl").write_text("")

    score = corpus.harness.replay(
        corpus.captured("S3"),
        "--permissive",
        "--allow-readonly",
        mutate_capsule=drop_tool_records,
        name="tool-not-served",
    )
    _record(corpus, "tool-not-served", score)
    assert score.replay.tool_bodies == 1
    assert score.replay.live["tool"] == 1
    assert score.verdict != "faithful"


def test_a_mutated_output_fails_the_outcome_check(corpus: Corpus) -> None:
    score = corpus.harness.replay(
        corpus.captured("S1"), extra={"FIDELITY_MUTATE_OUTPUT": "1"}, name="mutated-output"
    )
    _record(corpus, "mutated-output", score)
    assert score.replay.live == {"model": 0, "tool": 0, "network": 0, "side_effects": 0}
    assert score.replay.finished and score.replay.exit_code == score.capture.exit_code
    assert score.replay.output_digest != score.capture.output_digest
    assert not score.outcome_equal
    assert score.verdict == "diverged"


@pytest.mark.parametrize("sid", ["S6"])
def test_replacing_the_oracles_with_the_report_flips_the_verdict(corpus: Corpus, sid: str) -> None:
    """Harness mutation: scored from the report's own counters, the verdict changes.

    Tied to a defect the report still has, on purpose: S6 (a repeated file side effect
    has no counter or check). S5 (#52) and S7 (#49) served here until their fixes landed
    (2026-11-19). When S6 is fixed, move this control to a case the report still misses
    -- a harness whose oracles never disagree with the report proves nothing.
    """
    score = corpus.score(sid)
    mutated = score_from_report_only(score)
    corpus.controls[f"report-only:{sid}"] = {"oracle": score.verdict, "report_only": mutated}
    assert score.verdict == "live-unreported", "fixed? pick a case the report still misses"
    assert mutated != score.verdict


def test_the_report_only_scorer_agrees_on_a_faithful_scenario(corpus: Corpus) -> None:
    """The mutation flips only where the report is wrong, not everywhere."""
    score = corpus.score("S1")
    assert score_from_report_only(score) == score.verdict == "faithful"
