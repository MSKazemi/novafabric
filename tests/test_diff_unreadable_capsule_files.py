"""An unreadable or malformed ``capsule.yaml``/``env.lock`` is "cannot compare", not a crash.

``DiffEngine`` read both files with a bare ``yaml.safe_load(path.read_text())``,
so invalid YAML, non-UTF-8 bytes, an unreadable file, or a top level that is not
a mapping escaped as a traceback. Python exits 1 on an uncaught exception — the
code ADR-0303 reserves for "the comparison was made and found a difference" —
so under ``--assert-no-regressions`` a corrupt capsule read as a regression.

ADR-0303 Amendment 2: the engine raises :class:`CapsuleFileError` naming the
file and the reason; ``nova diff`` prints one ``cannot compare:`` line on stderr
and exits 2 in every output format, gate flag or not; ``GET /api/diff`` answers
422.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _help_assert import strip_ansi
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.diff import CapsuleFileError
from novafabric.diff._engine import DiffEngine

runner = CliRunner()

ENV_LOCK = "python:\n  version: '3.12.3'\nhost:\n  os: linux\n  arch: x86_64\n"


def _call(prompt: str) -> dict[str, Any]:
    return {
        "model_call_id": prompt,
        "parent_span_id": "root",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.request.messages": [{"role": "user", "content": prompt}],
    }


def _capsule(root: Path, run_id: str) -> Path:
    d = root / run_id
    (d / "outputs").mkdir(parents=True)
    (d / "capsule.yaml").write_text(f"run_id: {run_id}\nstatus: success\n")
    (d / "env.lock").write_text(ENV_LOCK)
    (d / "model-calls.jsonl").write_text(json.dumps(_call("x")) + "\n")
    (d / "tool-calls.jsonl").write_text("")
    return d


#: name -> (file, bytes written, a fragment the reason must contain)
CORRUPTIONS: dict[str, tuple[str, bytes, str]] = {
    "manifest-invalid-yaml": ("capsule.yaml", b"run_id: [unclosed\n", "not valid YAML"),
    "manifest-not-utf8": ("capsule.yaml", b"run_id: \xff\xfe\n", "not UTF-8"),
    "manifest-a-list": ("capsule.yaml", b"- just\n- a list\n", "not a mapping"),
    "env-invalid-yaml": ("env.lock", b"python: {version: [\n", "not valid YAML"),
    "env-not-utf8": ("env.lock", b"\xff\xfe\x00python: x\n", "not UTF-8"),
    "env-a-scalar": ("env.lock", b"python=3.12\n", "not a mapping"),
}


@pytest.fixture(params=sorted(CORRUPTIONS))
def corrupt(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Path, Path, str, str]:
    name, data, reason = CORRUPTIONS[request.param]
    a = _capsule(tmp_path, "run-a")
    b = _capsule(tmp_path, "run-b")
    (b / name).write_bytes(data)
    return a, b, name, reason


# ── the engine ───────────────────────────────────────────────────────────────


def test_engine_raises_the_named_error_with_file_and_reason(
    corrupt: tuple[Path, Path, str, str],
) -> None:
    a, b, name, reason = corrupt
    with pytest.raises(CapsuleFileError) as info:
        DiffEngine().compare(a, b)
    assert info.value.path == b / name
    assert reason in str(info.value)
    assert str(b / name) in str(info.value)


def test_an_unreadable_file_is_the_named_error(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "run-a")
    b = _capsule(tmp_path, "run-b")
    (b / "env.lock").unlink()
    (b / "env.lock").mkdir()  # exists, cannot be read as a file — on any uid
    with pytest.raises(CapsuleFileError, match="cannot be read"):
        DiffEngine().compare(a, b)


def test_a_non_mapping_section_of_a_differing_env_lock_is_the_named_error(
    tmp_path: Path,
) -> None:
    a = _capsule(tmp_path, "run-a")
    b = _capsule(tmp_path, "run-b")
    (b / "env.lock").write_text("python: '3.13'\nhost:\n  os: linux\n")
    with pytest.raises(CapsuleFileError, match=r"'python' is a str, not a mapping"):
        DiffEngine().compare(a, b)


def test_identical_env_locks_compare_whatever_their_shape(tmp_path: Path) -> None:
    """Equal parsed documents establish "no environment change" without reading fields."""
    a = _capsule(tmp_path, "run-a")
    b = _capsule(tmp_path, "run-b")
    for d in (a, b):
        (d / "env.lock").write_text("python: '3.12'\n")
    report = DiffEngine().compare(a, b)
    assert report.env_changes == []


def test_missing_and_empty_files_are_still_absent_not_malformed(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "run-a")
    b = _capsule(tmp_path, "run-b")
    (a / "env.lock").unlink()
    (b / "env.lock").write_text("")
    report = DiffEngine().compare(a, b)
    assert report.run_b_id == "run-b"
    assert report.env_changes == []


def test_clean_capsules_are_unchanged(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "run-a")
    b = _capsule(tmp_path, "run-b")
    report = DiffEngine().compare(a, b)
    assert (report.run_a_id, report.run_b_id) == ("run-a", "run-b")
    assert not report.has_changes


# ── the CLI ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["text", "json", "github-annotation"])
@pytest.mark.parametrize("gate", [False, True])
def test_cli_exits_2_with_one_clean_line_in_every_format(
    corrupt: tuple[Path, Path, str, str], fmt: str, gate: bool
) -> None:
    a, b, name, reason = corrupt
    args = ["diff", str(a), str(b), "--output-format", fmt]
    if gate:
        args.append("--assert-no-regressions")
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert result.stdout == "", "nothing on stdout: a CI step must not parse half a report"
    err = strip_ansi(result.stderr)
    assert err.startswith("cannot compare:"), err
    assert name in err and reason in err


def test_cli_graph_shape_does_not_mask_the_error(
    corrupt: tuple[Path, Path, str, str],
) -> None:
    a, b, _, _ = corrupt
    result = runner.invoke(app, ["diff", str(a), str(b), "--assert-same-shape"])
    assert result.exit_code == 2
    assert strip_ansi(result.stderr).startswith("cannot compare:")


def test_help_documents_a_malformed_capsule_file_as_cannot_compare() -> None:
    result = runner.invoke(app, ["diff", "--help"], terminal_width=200)
    text = " ".join(strip_ansi(result.output).split())
    assert "malformed capsule.yaml or env.lock" in text


# ── the API ──────────────────────────────────────────────────────────────────


def test_api_diff_answers_422_not_500(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    runs = tmp_path / "runs"
    _capsule(runs, "run-a")
    b = _capsule(runs, "run-b")
    (b / "env.lock").write_text("python: {version: [\n")
    token = "test-token-1234567890abcdef"
    app_ = create_app(
        token=token, capsule_dir=runs, db_path=tmp_path / "registry.db", static_dir=None
    )
    with TestClient(app_) as client:
        res = client.get(
            f"/api/diff?run_a=run-a&run_b=run-b&token={token}",
            headers={"host": "127.0.0.1:4321"},
        )
    assert res.status_code == 422, res.text
    assert "env.lock" in res.json()["detail"]
    assert "not valid YAML" in res.json()["detail"]
