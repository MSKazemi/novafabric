"""The eval writers that add to a finished capsule never write through a planted link.

``write_facet`` (``extensions/dev.novafabric.dataset-provenance/<name>.json``) and
``append_score`` / ``write_scores`` (``scores.jsonl``, reached from
``nova eval offline``, the annotation queue and the experiment runner) used plain
``Path.write_text`` / ``open("a")``: a link planted at the name -- by the recorded
workload, or by anyone who can write into the capsule -- redirected the write to
wherever it pointed, and a hard link carried an append out of the capsule.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from novafabric._capsule_fs import CapsuleLinkError, CapsuleSpecialFileError
from novafabric.eval.dataset_provenance import (
    FACET_DIRNAME,
    DatasetProvenanceFacet,
    write_facet,
)
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    append_score,
    read_scores,
    write_scores,
)

ORIGINAL = "OUTSIDE-ORIGINAL\n"


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    path = tmp_path / "caps" / "run-1"
    (path / "outputs").mkdir(parents=True)
    return path


@pytest.fixture()
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "outside.txt"
    path.write_text(ORIGINAL)
    return path


def _score(name: str = "accuracy") -> Score:
    return Score(
        subject="sha256:" + "d" * 64,
        subject_kind="capsule",
        name=name,
        value=True,
        value_type=ScoreValueType.BOOLEAN,
        source=ScoreSource.HUMAN,
        evaluator_id="tester",
        eval_card_digest="sha256:" + "c" * 64,
    )


def _facet() -> DatasetProvenanceFacet:
    return DatasetProvenanceFacet(
        name="My Eval Set",
        version="1",
        dataset_hash="sha256:" + "a" * 64,
        split_hash="sha256:" + "b" * 64,
    )


def test_append_score_refuses_a_symlink_and_never_writes_through(
    capsule: Path, outside: Path
) -> None:
    (capsule / SCORES_FILENAME).symlink_to(outside)
    with pytest.raises(CapsuleLinkError):
        append_score(capsule / SCORES_FILENAME, _score())
    assert outside.read_text() == ORIGINAL


def test_append_score_refuses_a_fifo_without_blocking(capsule: Path) -> None:
    os.mkfifo(capsule / SCORES_FILENAME)

    def _alarm(_signum: int, _frame: object) -> None:
        raise TimeoutError("append_score blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(10)
    try:
        with pytest.raises(CapsuleSpecialFileError):
            append_score(capsule / SCORES_FILENAME, _score())
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_append_score_gives_a_hard_link_a_private_copy(capsule: Path, outside: Path) -> None:
    os.link(outside, capsule / SCORES_FILENAME)
    append_score(capsule / SCORES_FILENAME, _score())
    assert outside.read_text() == ORIGINAL, "the append went through the hard link"
    target = capsule / SCORES_FILENAME
    assert target.stat().st_nlink == 1
    text = target.read_text()
    assert text.startswith(ORIGINAL) and '"accuracy"' in text


def test_append_score_still_appends_in_place(capsule: Path) -> None:
    target = capsule / SCORES_FILENAME
    append_score(target, _score("a"))
    inode = target.stat().st_ino
    append_score(target, _score("b"))
    assert target.stat().st_ino == inode  # a plain append, no rewrite (query cache D1)
    assert [s.name for s in read_scores(target)] == ["a", "b"]


def test_write_scores_replaces_a_link(capsule: Path, outside: Path) -> None:
    (capsule / SCORES_FILENAME).symlink_to(outside)
    write_scores(capsule / SCORES_FILENAME, [_score()])
    assert outside.read_text() == ORIGINAL
    assert not (capsule / SCORES_FILENAME).is_symlink()
    assert [s.name for s in read_scores(capsule / SCORES_FILENAME)] == ["accuracy"]


def test_write_facet_replaces_a_planted_facet_link(capsule: Path, outside: Path) -> None:
    facet_dir = capsule / FACET_DIRNAME
    facet_dir.mkdir(parents=True)
    (facet_dir / "my-eval-set.json").symlink_to(outside)
    (facet_dir / "hard.json").write_text("{}")
    out = write_facet(capsule, _facet())
    assert outside.read_text() == ORIGINAL
    assert out == facet_dir / "my-eval-set.json"
    assert not out.is_symlink() and '"My Eval Set"' in out.read_text()


def test_write_facet_replaces_a_hard_link(capsule: Path, outside: Path) -> None:
    facet_dir = capsule / FACET_DIRNAME
    facet_dir.mkdir(parents=True)
    os.link(outside, facet_dir / "my-eval-set.json")
    write_facet(capsule, _facet())
    assert outside.read_text() == ORIGINAL


def test_write_facet_refuses_a_linked_extensions_dir(capsule: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (capsule / "extensions").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(CapsuleLinkError):
        write_facet(capsule, _facet())
    assert list(elsewhere.iterdir()) == []


def test_write_facet_creates_the_namespace(capsule: Path) -> None:
    out = write_facet(capsule, _facet())
    assert out.is_file() and out.parent == capsule / FACET_DIRNAME
