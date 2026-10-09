"""``nova diff --json`` is exactly ``--output-format json``.

``--help`` listed ``--json`` ("Emit the diff record as JSON.") but only the
``--media`` and ``--significance`` modes read it: a capsule or ``name@version``
diff given ``--json`` printed the text report, so a CI step parsing it failed on
the first byte. Conversely ``--output-format json`` was ignored by ``--media``
and ``--significance``. The two spellings are now one setting in every mode, and
asking for JSON and another format at once is a usage error (exit 2, ADR-0303).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from _help_assert import strip_ansi
from typer.testing import CliRunner

import novafabric.cli.diff as diff_cli
from novafabric.cli.main import app
from novafabric.eval.scores import Score, ScoreSource, ScoreValueType, write_scores

runner = CliRunner()

_DIGEST = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _capsule(tmp_path: Path, name: str, stdout: str = "output\n") -> Path:
    cap = tmp_path / name
    (cap / "inputs").mkdir(parents=True)
    (cap / "outputs").mkdir()
    (cap / "capsule.yaml").write_text(
        yaml.dump({"schema_version": "0.1.0", "run_id": name.upper(), "status": "success"})
    )
    (cap / "env.lock").write_text(
        yaml.dump({"python": {"version": "3.12.3"}, "host": {"os": "linux"}})
    )
    (cap / "model-calls.jsonl").write_text("")
    (cap / "tool-calls.jsonl").write_text("")
    (cap / "outputs" / "stdout.txt").write_text(stdout)
    return cap


def _media_capsule(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    raw = b"same"
    part = {
        "type": "image",
        "media": {
            "media_type": "image/png",
            "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "redacted": False,
            "blob_ref": "m.png",
        },
    }
    d.joinpath("model-calls.jsonl").write_text(
        json.dumps({"gen_ai.request.messages": [{"role": "user", "content": [part]}]}) + "\n"
    )
    d.joinpath("m.png").write_bytes(raw)
    return d


def _scores(path: Path, successes: int, n: int) -> Path:
    write_scores(
        path,
        [
            Score(
                subject=_DIGEST,
                name="task_pass",
                value=i < successes,
                value_type=ScoreValueType.BOOLEAN,
                source=ScoreSource.CODE,
                evaluator_id="ev",
                eval_card_digest=_DIGEST,
            )
            for i in range(n)
        ],
    )
    return path


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["diff", *args])


def test_json_flag_emits_the_capsule_diff_report(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a")
    b = _capsule(tmp_path, "b", stdout="different\n")
    flag = _invoke(str(a), str(b), "--json")
    long = _invoke(str(a), str(b), "--output-format", "json")
    assert flag.exit_code == long.exit_code == 0, flag.output
    doc = json.loads(flag.stdout)
    assert doc["has_changes"] is True
    assert doc == json.loads(long.stdout)


def test_json_flag_keeps_the_gate(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a")
    b = _capsule(tmp_path, "b", stdout="different\n")
    result = _invoke(str(a), str(b), "--json", "--assert-no-regressions")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["has_changes"] is True


def test_json_flag_emits_the_asset_diff_document(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = {"1": {"temperature": 0.1}, "2": {"temperature": 0.9}}
    monkeypatch.setattr(
        diff_cli, "get_asset", lambda name, version: {"spec_json": json.dumps(specs[version])}
    )
    flag = _invoke("agent@1", "agent@2", "--json")
    long = _invoke("agent@1", "agent@2", "--output-format", "json")
    assert flag.exit_code == long.exit_code == 0, flag.output
    assert json.loads(flag.stdout) == json.loads(long.stdout)
    assert json.loads(flag.stdout)["changed"] == {"temperature": {"from": 0.1, "to": 0.9}}


def test_json_flag_with_group_by_wraps_the_report(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a")
    b = _capsule(tmp_path, "b")
    result = _invoke("--group-by", "variant", str(a), str(b), "--json")
    assert result.exit_code == 0, result.output
    assert set(json.loads(result.stdout)) >= {"variant_groups", "cross_arm", "diff"}


def test_output_format_json_works_for_media(tmp_path: Path) -> None:
    a = _media_capsule(tmp_path, "a")
    b = _media_capsule(tmp_path, "b")
    flag = _invoke("--media", str(a), str(b), "--json")
    long = _invoke("--media", str(a), str(b), "--output-format", "json")
    assert flag.exit_code == long.exit_code == 0, long.output
    assert json.loads(long.stdout) == json.loads(flag.stdout)


def test_output_format_json_works_for_significance(tmp_path: Path) -> None:
    base = _scores(tmp_path / "base.jsonl", 48, 50)
    cand = _scores(tmp_path / "cand.jsonl", 48, 50)
    common = ("--significance", "--baseline", str(base), "--candidate", str(cand))
    flag = _invoke(*common, "--json")
    long = _invoke(*common, "--output-format", "json")
    assert flag.exit_code == long.exit_code == 0, long.output
    volatile = {"diff_id", "created_at"}  # minted per invocation
    a, b = json.loads(long.stdout), json.loads(flag.stdout)
    assert {k: v for k, v in a.items() if k not in volatile} == {
        k: v for k, v in b.items() if k not in volatile
    }
    assert "sprt" in a


@pytest.mark.parametrize("other", ["text", "github-annotation"])
def test_json_flag_with_another_explicit_format_is_a_usage_error(
    tmp_path: Path, other: str
) -> None:
    a = _capsule(tmp_path, "a")
    b = _capsule(tmp_path, "b")
    result = _invoke(str(a), str(b), "--json", "--output-format", other)
    assert result.exit_code == 2
    assert "--json" in result.output and other in result.output


def test_json_flag_with_explicit_json_format_is_fine(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a")
    b = _capsule(tmp_path, "b")
    result = _invoke(str(a), str(b), "--json", "--output-format", "json")
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)


@pytest.mark.parametrize("mode", ["--media", "--significance"])
def test_github_annotation_is_refused_where_it_has_no_meaning(
    tmp_path: Path, mode: str
) -> None:
    """--media and --significance print text or JSON only; silently printing
    text for a requested annotation format is the bug class this file guards."""
    a = _media_capsule(tmp_path, "a")
    b = _media_capsule(tmp_path, "b")
    args = [mode, str(a), str(b)] if mode == "--media" else [
        mode, "--baseline", str(a), "--candidate", str(b)
    ]
    result = _invoke(*args, "--output-format", "github-annotation")
    assert result.exit_code == 2
    assert "github-annotation" in result.output


def test_help_describes_json_as_the_output_format_alias() -> None:
    result = runner.invoke(app, ["diff", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    text = strip_ansi(result.output)
    line = next(ln for ln in text.splitlines() if "--json" in ln and "│" in ln)
    assert "--output-format json" in line
