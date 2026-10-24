"""A score correction (``supersedes``, ADR-0119) replaces the record it corrects.

Review eval-adapters-query-09: the original and its correction were both
aggregated, so a reviewer correcting 0.1 -> 0.9 made ``avg(score[faithfulness])``
read 0.5 in ``nova query``, and every other reader kept the original too.
"""
from __future__ import annotations

from novafabric.eval.scores import Score, ScoreSource, ScoreValueType, effective_scores

_SHA = "sha256:" + "a" * 64


def _score(score_id: str, value: float, supersedes: str | None = None, name: str = "f") -> Score:
    return Score(
        score_id=score_id,
        subject=_SHA,
        subject_kind="capsule",
        name=name,
        value=value,
        value_type=ScoreValueType.NUMERIC,
        source=ScoreSource.HUMAN,
        evaluator_id="rev",
        eval_card_digest=_SHA,
        supersedes=supersedes,
    )


A, B, C, D = (f"01HZZZZZZZZZZZZZZZZZZZZZZ{c}" for c in "ABCD")


def test_a_correction_replaces_the_original() -> None:
    assert [s.score_id for s in effective_scores([_score(A, 0.1), _score(B, 0.9, A)])] == [B]


def test_a_chain_of_corrections_keeps_only_the_last() -> None:
    chain = [_score(A, 0.1), _score(B, 0.5, A), _score(C, 0.9, B)]
    assert [s.score_id for s in effective_scores(chain)] == [C]


def test_unrelated_scores_keep_their_order() -> None:
    scores = [_score(A, 0.1, name="x"), _score(B, 0.2, name="y"), _score(C, 0.3, A), _score(D, 1)]
    assert [s.score_id for s in effective_scores(scores)] == [B, C, D]


def test_the_significance_gate_reads_the_correction() -> None:
    from novafabric.eval.scores import boolean_metric_outcomes

    def flag(score_id: str, value: bool, supersedes: str | None = None) -> Score:
        return _score(score_id, 0.0, supersedes).model_copy(
            update={"value": value, "value_type": ScoreValueType.BOOLEAN}
        )

    scores = [flag(A, False), flag(B, True), flag(C, True, A)]
    assert boolean_metric_outcomes(scores, "f") == [1, 1]


def test_nova_diff_significance_reads_the_correction(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from novafabric.cli.diff import _read_outcomes
    from novafabric.eval.scores import append_score

    path = tmp_path / "scores.jsonl"
    for s in (
        _score(A, 0.0).model_copy(update={"value": False, "value_type": ScoreValueType.BOOLEAN}),
        _score(B, 0.0, A).model_copy(update={"value": True, "value_type": ScoreValueType.BOOLEAN}),
    ):
        append_score(path, s)
    assert _read_outcomes(path, "f") == [1]
