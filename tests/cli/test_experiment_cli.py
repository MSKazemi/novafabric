"""CLI tests for ``nova experiment`` (experimental, ADR-0120).

Smoke of the full vertical slice: run over a stub command (one capsule per
item), store, list/show, compare with the ADR-0080 exit-code contract, and the
``run --baseline`` CI gate. Everything local and offline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_ECHO_CODE = "print('{input}')"
_WRONG_CODE = "print('never-the-expected-answer')"


def _dataset(tmp_path: Path, n: int, name: str = "items.jsonl") -> Path:
    path = tmp_path / name
    rows = [
        json.dumps({"item_id": f"i{k}", "input": f"answer-{k}", "expected": f"answer-{k}"})
        for k in range(n)
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _run(tmp_path: Path, dataset: Path, code: str, *extra: str) -> object:
    return runner.invoke(
        app,
        [
            "experiment",
            "run",
            "--dataset",
            str(dataset),
            "--target",
            "stub-agent@1.0.0",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--experiments-dir",
            str(tmp_path / "exps"),
            *extra,
            sys.executable,
            "-c",
            code,
        ],
    )


def _stored_ids(tmp_path: Path) -> list[str]:
    return sorted(p.stem for p in (tmp_path / "exps").glob("*.json"))


def test_run_list_show_compare_roundtrip(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, 3)

    baseline = _run(tmp_path, dataset, _ECHO_CODE)
    assert baseline.exit_code == 0, baseline.output
    assert "finalized" in baseline.output
    assert "3/3 ok" in baseline.output

    candidate = _run(tmp_path, dataset, _WRONG_CODE)
    assert candidate.exit_code == 0, candidate.output

    base_id, cand_id = _stored_ids(tmp_path)[0], _stored_ids(tmp_path)[1]

    listed = runner.invoke(
        app, ["experiment", "list", "--experiments-dir", str(tmp_path / "exps"), "--json"]
    )
    assert listed.exit_code == 0
    listed_ids = [e["experiment_id"] for e in json.loads(listed.output)]
    assert listed_ids == [base_id, cand_id]

    table = runner.invoke(
        app, ["experiment", "list", "--experiments-dir", str(tmp_path / "exps")]
    )
    assert table.exit_code == 0
    assert "finalized" in table.output

    shown = runner.invoke(
        app,
        ["experiment", "show", base_id, "--experiments-dir", str(tmp_path / "exps"), "--json"],
    )
    assert shown.exit_code == 0
    record = json.loads(shown.output)
    assert record["status"] == "finalized"
    assert record["dataset_ref"]["dataset_hash"].startswith("sha256:")
    assert len(record["runs"]) == 3

    # all-fail candidate over 3 items crosses the SPRT boundary -> exit 3
    compared = runner.invoke(
        app,
        [
            "experiment",
            "compare",
            base_id,
            cand_id,
            "--experiments-dir",
            str(tmp_path / "exps"),
            "--json",
        ],
    )
    assert compared.exit_code == 3, compared.output
    comparison = json.loads(compared.output)
    assert comparison["exit_code"] == 3
    assert comparison["significance"]["sprt"]["verdict"] == "accept_h1"

    # same experiment vs itself -> no regression, exit 0
    same = runner.invoke(
        app,
        ["experiment", "compare", base_id, base_id, "--experiments-dir", str(tmp_path / "exps")],
    )
    assert same.exit_code == 0, same.output


def test_run_with_baseline_gates_ci(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, 3)
    baseline = _run(tmp_path, dataset, _ECHO_CODE)
    assert baseline.exit_code == 0, baseline.output
    base_id = _stored_ids(tmp_path)[0]

    gated = _run(tmp_path, dataset, _WRONG_CODE, "--baseline", base_id)
    assert gated.exit_code == 3, gated.output
    assert "accept_h1" in gated.output

    # the gated run recorded its baseline for provenance
    new_id = next(i for i in _stored_ids(tmp_path) if i != base_id)
    record = json.loads((tmp_path / "exps" / f"{new_id}.json").read_text())
    assert record["baseline_experiment_id"] == base_id


def test_compare_dataset_mismatch_exits_2(tmp_path: Path) -> None:
    ds_a = _dataset(tmp_path, 1, "a.jsonl")
    ds_b = _dataset(tmp_path, 2, "b.jsonl")
    assert _run(tmp_path, ds_a, _ECHO_CODE).exit_code == 0
    assert _run(tmp_path, ds_b, _ECHO_CODE).exit_code == 0
    id_a, id_b = _stored_ids(tmp_path)
    result = runner.invoke(
        app,
        ["experiment", "compare", id_a, id_b, "--experiments-dir", str(tmp_path / "exps")],
    )
    assert result.exit_code == 2
    assert "same pinned dataset" in result.output

    # run --baseline against an experiment over a different dataset: same hard error
    gated = _run(tmp_path, ds_a, _ECHO_CODE, "--baseline", id_b)
    assert gated.exit_code == 2
    assert "same pinned dataset" in gated.output


def test_run_writes_out_file_and_show_renders_items(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, 1)
    out = tmp_path / "artifacts" / "experiment.json"
    result = _run(tmp_path, dataset, _ECHO_CODE, "--out", str(out))
    assert result.exit_code == 0, result.output
    record = json.loads(out.read_text())
    assert record["status"] == "finalized"

    shown = runner.invoke(
        app,
        [
            "experiment",
            "show",
            record["experiment_id"],
            "--experiments-dir",
            str(tmp_path / "exps"),
        ],
    )
    assert shown.exit_code == 0
    assert "i0" in shown.output


def test_run_with_missing_baseline_exits_2(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, 1)
    result = _run(tmp_path, dataset, _ECHO_CODE, "--baseline", "01HXAY7M7QM4YZ2K7N9DPBYK2W")
    assert result.exit_code == 2
    assert "no experiment" in result.output


def test_compare_bad_hypotheses_and_out_file(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, 1)
    assert _run(tmp_path, dataset, _ECHO_CODE).exit_code == 0
    (exp_id,) = _stored_ids(tmp_path)
    bad = runner.invoke(
        app,
        [
            "experiment",
            "compare",
            exp_id,
            exp_id,
            "--p0",
            "0.2",
            "--p1",
            "0.8",  # violates 0 < p1 < p0 < 1
            "--experiments-dir",
            str(tmp_path / "exps"),
        ],
    )
    assert bad.exit_code == 2

    out = tmp_path / "cmp" / "comparison.json"
    ok = runner.invoke(
        app,
        [
            "experiment",
            "compare",
            exp_id,
            exp_id,
            "--experiments-dir",
            str(tmp_path / "exps"),
            "--out",
            str(out),
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(out.read_text())["exit_code"] == 0


def test_list_corrupt_store_exits_2(tmp_path: Path) -> None:
    (tmp_path / "exps").mkdir(parents=True)
    (tmp_path / "exps" / "01HXAY7M7QM4YZ2K7N9DPBYK2W.json").write_text("{broken")
    result = runner.invoke(
        app, ["experiment", "list", "--experiments-dir", str(tmp_path / "exps")]
    )
    assert result.exit_code == 2
    assert "invalid experiment record" in result.output


def test_run_invalid_dataset_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    result = _run(tmp_path, bad, _ECHO_CODE)
    assert result.exit_code == 2
    assert "invalid JSON" in result.output


def test_show_missing_experiment_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "experiment",
            "show",
            "01HXAY7M7QM4YZ2K7N9DPBYK2W",
            "--experiments-dir",
            str(tmp_path / "exps"),
        ],
    )
    assert result.exit_code == 2
    assert "no experiment" in result.output


def test_list_empty_store(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["experiment", "list", "--experiments-dir", str(tmp_path / "exps")]
    )
    assert result.exit_code == 0
    assert "No experiments recorded" in result.output
