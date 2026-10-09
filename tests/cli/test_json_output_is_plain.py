"""Machine-readable CLI output stays parseable when colour is forced.

``Console.print_json`` syntax-highlights whenever colour is forced — ``FORCE_COLOR``
or Typer/Rich detecting GitHub Actions — so ``nova diff --significance
--output-format json`` (and 37 other call sites) wrote ANSI escapes into
stdout in CI and into ``> out.json`` redirects. ``Console.print`` of a JSON string
(``nova subject-proof``) was worse: it also hard-wrapped long lines at the terminal
width, so the report did not parse in a narrow terminal even without colour.

Two guards:

* static — no ``print_json`` anywhere in the package, and no Rich ``.print`` of a
  JSON-serialised value under ``novafabric.cli``; JSON goes through
  ``novafabric.cli._output.emit_json`` (or a plain ``typer.echo``/``print``);
* behavioural — a representative set of JSON-emitting commands, run as real
  subprocesses (Rich decides colour when its module-level consoles are built, so
  an in-process ``monkeypatch.setenv`` would not exercise the CI path) under
  ``FORCE_COLOR=1 COLUMNS=40``, must print stdout that ``json.loads`` accepts.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

import novafabric
from novafabric.eval.scores import Score, ScoreSource, ScoreValueType, write_scores

_PKG = Path(novafabric.__file__).parent
_CLI = _PKG / "cli"

# Calls whose result is a JSON document (or that render one).
_JSON_PRODUCERS = frozenset({"dumps", "model_dump_json", "to_json", "format_json"})


# ── static guard ──────────────────────────────────────────────────────────────


def _is_json_producer(expr: ast.AST) -> bool:
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _JSON_PRODUCERS:
                return True
    return False


def print_json_calls(source: str) -> list[int]:
    """Line numbers of every ``<anything>.print_json(...)`` call in *source*."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "print_json"
    ]


def console_prints_of_json(source: str) -> list[int]:
    """Line numbers where a Rich console ``.print``s a JSON-serialised value.

    Flags ``console.print(json.dumps(...))`` and ``console.print(text)`` where
    ``text`` was assigned from a JSON producer in the same function. The receiver
    must name a console (``console``, ``err_console``, ``Console(...)``), so plain
    ``print``/``typer.echo`` — the correct spellings — never match.
    """
    hits: list[int] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        json_names = {
            target.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign) and _is_json_producer(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(fn):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "print"
                and "console" in ast.unparse(node.func.value).lower()
            ):
                continue
            for arg in node.args:
                if (isinstance(arg, ast.Name) and arg.id in json_names) or _is_json_producer(
                    arg
                ):
                    hits.append(node.lineno)
    return hits


def test_detectors_flag_the_shapes_that_corrupted_output() -> None:
    # The guard must be able to fail: each shape that shipped is detected ...
    assert print_json_calls("console.print_json(diff.model_dump_json())\n") == [1]
    assert console_prints_of_json(
        "def f(report):\n    text = json.dumps(report, indent=2)\n    console.print(text)\n"
    ) == [3]
    assert console_prints_of_json("def f(r):\n    err_console.print(json.dumps(r))\n") == [2]
    # ... and the correct spellings are not.
    assert print_json_calls("emit_json(diff.model_dump_json())\n") == []
    assert console_prints_of_json(
        "def f(r):\n    text = json.dumps(r)\n    typer.echo(text)\n    print(text)\n"
        "    console.print(f'[green]ok[/green] {r}')\n"
    ) == []


def test_no_print_json_anywhere_in_the_package() -> None:
    offenders = [
        f"{path.relative_to(_PKG.parent)}:{line}"
        for path in sorted(_PKG.rglob("*.py"))
        for line in print_json_calls(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "Console.print_json emits ANSI when colour is forced (FORCE_COLOR, CI); "
        "use novafabric.cli._output.emit_json: " + ", ".join(offenders)
    )


def test_no_rich_console_prints_json_in_the_cli() -> None:
    offenders = [
        f"{path.relative_to(_PKG.parent)}:{line}"
        for path in sorted(_CLI.rglob("*.py"))
        for line in console_prints_of_json(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "Console.print wraps JSON at the terminal width, reads [..] as markup and "
        "colours it when forced; use novafabric.cli._output.emit_json or typer.echo: "
        + ", ".join(offenders)
    )


# ── behavioural guard ─────────────────────────────────────────────────────────

_DIGEST = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _capsule(root: Path, name: str, stdout: str = "output\n") -> Path:
    cap = root / name
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


def _media_capsule(root: Path, name: str) -> Path:
    cap = _capsule(root, name)
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
    (cap / "model-calls.jsonl").write_text(
        json.dumps(
            {"call_id": "c1", "gen_ai.request.messages": [{"role": "user", "content": [part]}]}
        )
        + "\n"
    )
    (cap / "m.png").write_bytes(raw)
    return cap


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


def _diff_significance(tmp: Path) -> list[str]:
    base = _scores(tmp / "base.jsonl", 48, 50)
    cand = _scores(tmp / "cand.jsonl", 40, 50)
    return ["diff", "--significance", "--baseline", str(base), "--candidate", str(cand),
            "--output-format", "json"]  # fmt: skip


def _diff_media(tmp: Path) -> list[str]:
    return ["diff", "--media", str(_media_capsule(tmp, "a")), str(_media_capsule(tmp, "b")),
            "--json"]  # fmt: skip


def _diff_capsules(tmp: Path) -> list[str]:
    a = _capsule(tmp, "a")
    b = _capsule(tmp, "b", stdout="a much longer different output line\n")
    return ["diff", str(a), str(b), "--json"]


def _media_list(tmp: Path) -> list[str]:
    return ["media", "list", "--json", str(_media_capsule(tmp, "m"))]


def _aibom_validate(tmp: Path) -> list[str]:
    bom = tmp / "bom.json"
    # A component name that is also Rich markup: Console.print would eat it.
    bom.write_text(json.dumps({"bomFormat": "CycloneDX", "components": [{"name": "[red]x"}]}))
    return ["aibom", "validate", str(bom), "--json"]


def _subject_proof(tmp: Path) -> list[str]:
    # No redaction index under NOVAFABRIC_HOME: the legal-hold report, warning on stderr.
    return ["subject-proof", "user@example.com"]


def _verify(tmp: Path) -> list[str]:
    return ["verify", str(_capsule(tmp, "v")), "--json"]


def _cost_usage_breakdown(tmp: Path) -> list[str]:
    usage = tmp / "usage.json"
    usage.write_text(json.dumps({"input_tokens": 1200, "output_tokens": 345}))
    return ["cost", "usage-breakdown", str(usage), "--json"]


_COMMANDS: dict[str, Callable[[Path], list[str]]] = {
    "diff --significance --output-format json": _diff_significance,
    "diff --media --json": _diff_media,
    "diff <capsule> <capsule> --json": _diff_capsules,
    "media list --json": _media_list,
    "aibom validate --json": _aibom_validate,
    "subject-proof": _subject_proof,
    "verify --json": _verify,
    "cost usage-breakdown --json": _cost_usage_breakdown,
}


@pytest.mark.parametrize("build", list(_COMMANDS.values()), ids=list(_COMMANDS))
def test_json_stdout_parses_with_colour_forced_in_a_narrow_terminal(
    tmp_path: Path, build: Callable[[Path], list[str]]
) -> None:
    args = build(tmp_path)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"NO_COLOR", "COLORTERM", "NOVAFABRIC_CAPSULE_DIR", "GITHUB_ACTIONS"}
    }
    env |= {
        "FORCE_COLOR": "1",
        "COLUMNS": "40",
        "NOVAFABRIC_HOME": str(tmp_path / "home"),
        "NOVA_PII_PEPPER": "test-pepper",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "novafabric.cli.main", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    assert proc.stdout.strip(), f"no stdout (exit {proc.returncode}): {proc.stderr[-2000:]}"
    assert "\x1b[" not in proc.stdout, proc.stdout[:500]
    json.loads(proc.stdout)  # the whole of stdout is one JSON document
