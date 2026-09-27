"""CLI smoke for the ADR-0117 P4 aggregation pin on ``nova experiment``.

``run --score-config`` resolves the ref read-only against the local catalog
(``NOVAFABRIC_DB_PATH`` redirected per test), records ``name@version`` + digest,
and fails with exit 2 — before any item runs — on an unresolvable ref.
``compare`` reports the ``score_config`` block; ``--require-comparable`` turns
anything but a shared digest into exit 2. Assertions use ``--json`` or
whitespace-squashed text (Rich wraps at a variable width).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from _help_assert import assert_flag_in_help, strip_ansi
from typer.testing import CliRunner, Result

from novafabric.cli.main import app
from novafabric.eval.score_config_catalog import register_config
from novafabric.eval.scores import ScoreValueType

runner = CliRunner()

_ECHO_CODE = "print('{input}')"


@pytest.fixture(autouse=True)
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "nfhome" / "registry.db"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nfhome"))
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    register_config("exact_match", ScoreValueType.BOOLEAN, "strict equality", db_path=db)
    register_config("exact_match", ScoreValueType.BOOLEAN, "stripped equality", db_path=db)
    return db


def _squash(text: str) -> str:
    # strip_ansi first: Rich emits escape sequences INSIDE option names when
    # colour is on (CI enables it), so collapsing whitespace alone leaves
    # "--flag" unmatchable. See tests/_help_assert.py / issue #21.
    return " ".join(strip_ansi(text).split())


def _dataset(tmp_path: Path) -> Path:
    path = tmp_path / "items.jsonl"
    rows = [
        json.dumps({"item_id": f"i{k}", "input": f"a{k}", "expected": f"a{k}"}) for k in range(2)
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _run(tmp_path: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "experiment",
            "run",
            "--dataset",
            str(_dataset(tmp_path)),
            "--target",
            "stub-agent@1.0.0",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--experiments-dir",
            str(tmp_path / "exps"),
            *extra,
            sys.executable,
            "-c",
            _ECHO_CODE,
        ],
    )


def _records(tmp_path: Path) -> list[dict[str, object]]:
    records = [json.loads(p.read_text()) for p in (tmp_path / "exps").glob("*.json")]
    return sorted(records, key=lambda r: str(r["created_at"]))


def _compare(tmp_path: Path, base: str, cand: str, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "experiment",
            "compare",
            base,
            cand,
            "--experiments-dir",
            str(tmp_path / "exps"),
            *extra,
        ],
    )


def test_run_pins_and_compare_reports_comparable(tmp_path: Path) -> None:
    first = _run(tmp_path, "--score-config", "exact_match@1")
    assert first.exit_code == 0, first.output
    assert "exact_match@1" in _squash(first.output)
    second = _run(tmp_path, "--score-config", "exact_match@1")
    assert second.exit_code == 0, second.output
    base, cand = _records(tmp_path)
    assert base["score_config_ref"] == "exact_match@1"
    assert str(base["score_config_digest"]).startswith("sha256:")
    assert base["score_config_digest"] == cand["score_config_digest"]

    result = _compare(tmp_path, str(base["experiment_id"]), str(cand["experiment_id"]), "--json")
    assert result.exit_code == 0, result.output
    block = json.loads(result.output)["score_config"]
    assert block["comparable"] is True and block["status"] == "same_digest"

    strict = _compare(
        tmp_path, str(base["experiment_id"]), str(cand["experiment_id"]), "--require-comparable"
    )
    assert strict.exit_code == 0, strict.output
    assert "score config: comparable" in _squash(strict.output)


def test_different_digests_not_comparable_and_strict_fails(tmp_path: Path) -> None:
    assert _run(tmp_path, "--score-config", "exact_match@1").exit_code == 0
    assert _run(tmp_path, "--score-config", "exact_match").exit_code == 0  # latest = @2
    base, cand = _records(tmp_path)
    assert cand["score_config_ref"] == "exact_match@2"

    result = _compare(tmp_path, str(base["experiment_id"]), str(cand["experiment_id"]))
    assert result.exit_code == 0, result.output  # reported, not gated by default
    assert "NOT comparable" in _squash(result.output)

    strict = _compare(
        tmp_path, str(base["experiment_id"]), str(cand["experiment_id"]), "--require-comparable"
    )
    assert strict.exit_code == 2
    assert "--require-comparable" in _squash(strict.output)


def test_unpinned_side_is_null_and_strict_fails(tmp_path: Path) -> None:
    assert _run(tmp_path).exit_code == 0
    assert _run(tmp_path, "--score-config", "exact_match@1").exit_code == 0
    base, cand = _records(tmp_path)
    result = _compare(tmp_path, str(base["experiment_id"]), str(cand["experiment_id"]), "--json")
    block = json.loads(result.output)["score_config"]
    assert block["comparable"] is None and block["status"] == "not_pinned"

    text = _compare(tmp_path, str(base["experiment_id"]), str(cand["experiment_id"]))
    assert "not pinned" in _squash(text.output)


def test_run_baseline_require_comparable_gates(tmp_path: Path) -> None:
    assert _run(tmp_path, "--score-config", "exact_match@1").exit_code == 0
    (base,) = _records(tmp_path)
    gated = _run(
        tmp_path,
        "--score-config",
        "exact_match@2",
        "--baseline",
        str(base["experiment_id"]),
        "--require-comparable",
    )
    assert gated.exit_code == 2, gated.output
    assert "NOT directly comparable" in _squash(gated.output)

    ok = _run(
        tmp_path,
        "--score-config",
        "exact_match@1",
        "--baseline",
        str(base["experiment_id"]),
        "--require-comparable",
    )
    assert ok.exit_code == 0, ok.output


@pytest.mark.parametrize(
    ("ref", "fragment"),
    [("exact_match@9", "no score config registered"), ("nope@x", "invalid config ref")],
)
def test_unresolvable_score_config_exits_2_before_running(
    tmp_path: Path, ref: str, fragment: str
) -> None:
    result = _run(tmp_path, "--score-config", ref)
    assert result.exit_code == 2
    assert fragment in _squash(result.output)
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "exps").exists()


def test_help_lists_new_flags() -> None:
    run_help = runner.invoke(app, ["experiment", "run", "--help"])
    assert_flag_in_help(run_help, "--score-config")
    compare_help = runner.invoke(app, ["experiment", "compare", "--help"])
    assert_flag_in_help(compare_help, "--require-comparable")
