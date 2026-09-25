# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``nova hitl`` CLI smoke + failure paths (ADR-0150 P1/P2, read-only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from novafabric.cli import hitl as hitl_cli
from novafabric.cli.main import app

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "conversation"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hitl_cli, "console", Console(width=250))


def _capsule_dir(tmp_path: Path, fixture: str, mutate: Any = None) -> Path:
    data = json.loads((FIXTURES / fixture).read_text())
    if mutate is not None:
        mutate(data)
    capsule = tmp_path / "cap"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return capsule


def _run(*args: str) -> Any:
    return runner.invoke(app, ["hitl", *args])


def _squash(text: str) -> str:
    return " ".join(text.split())


def _json(result: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.stdout)
    return data


# ── help smoke ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [[], ["thread"], ["context"], ["override"], ["rationale"], ["context", "verify"]],
)
def test_help(path: list[str]) -> None:
    result = _run(*path, "--help")
    assert result.exit_code == 0
    assert "Usage" in result.stdout


# ── thread show ───────────────────────────────────────────────────────────


def test_thread_show_json(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "accountability-capsule.json")
    result = _run("thread", "show", "--capsule", str(cap), "--json")
    assert result.exit_code == 0, result.stdout
    data = _json(result)
    assert [t["turn_id"] for t in data["turns"]] == ["t0", "t1", "t2"]
    assert data["broken_parent_refs"] == []
    assert "does not adjudicate" in data["notice"]


def test_thread_show_table_and_broken_parent(tmp_path: Path) -> None:
    def breaker(d: dict[str, Any]) -> None:
        d["facets"]["conversation"]["turns"][1]["parent_turn_id"] = "ghost"

    cap = _capsule_dir(tmp_path, "threaded-capsule.json", breaker)
    result = _run("thread", "show", "--capsule", str(cap))
    assert result.exit_code == 1
    assert "BROKEN PARENT" in result.stdout
    assert "does not adjudicate" in _squash(result.stdout)


def test_thread_show_duplicate_turn(tmp_path: Path) -> None:
    def dup(d: dict[str, Any]) -> None:
        turns = d["facets"]["conversation"]["turns"]
        turns.append({**turns[0], "at": "2026-07-15T10:01:00Z"})

    cap = _capsule_dir(tmp_path, "threaded-capsule.json", dup)
    result = _run("thread", "show", "--capsule", str(cap))
    assert result.exit_code == 1
    assert "DUPLICATE" in result.stdout


def test_thread_show_without_facet(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "valid-text-only-capsule.json")
    assert "No conversation facet" in _run("thread", "show", "--capsule", str(cap)).stdout
    result = _run("thread", "show", "--capsule", str(cap), "--json")
    assert result.exit_code == 0
    assert _json(result)["conversation"] is None


def test_capsule_resolves_by_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    result = _run("thread", "show", "--capsule", cap.name, "--json")
    assert result.exit_code == 0, result.output


# ── context show / verify ─────────────────────────────────────────────────


def test_context_show_ok(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "accountability-capsule.json")
    result = _run("context", "show", "--capsule", str(cap), "--turn", "t2", "--json")
    assert result.exit_code == 0
    data = _json(result)
    assert data["ok"] is True
    assert data["receipts"][0]["verified"]["root_matches"] is True
    text = _run("context", "show", "--capsule", str(cap), "--turn", "t2")
    assert text.exit_code == 0
    assert "MATCH" in text.stdout and "tool_output" in text.stdout


def test_context_show_shows_nf086_ref(tmp_path: Path) -> None:
    ref = "sha256:" + "1" * 64

    def add(d: dict[str, Any]) -> None:
        d["facets"]["conversation"]["decision_context"][0]["nf086_approval_ref"] = ref

    cap = _capsule_dir(tmp_path, "accountability-capsule.json", add)
    result = _run("context", "show", "--capsule", str(cap), "--turn", "t2")
    assert ref in result.stdout


def test_context_show_dangling_turn(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "invalid-dangling-ref-capsule.json")
    result = _run("context", "show", "--capsule", str(cap), "--turn", "t9")
    assert result.exit_code == 1
    assert "Dangling turn_ref: t9" in _squash(result.stdout)


def test_context_show_missing_receipt(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "accountability-capsule.json")
    result = _run("context", "show", "--capsule", str(cap), "--turn", "t0")
    assert result.exit_code == 1
    assert "No decision-context receipt" in result.stdout


def test_context_show_tampered(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "invalid-tampered-context-capsule.json")
    result = _run("context", "show", "--capsule", str(cap), "--turn", "t2")
    assert result.exit_code == 1
    assert "MISMATCH" in result.stdout


@pytest.mark.parametrize(
    ("fixture", "code", "status"),
    [
        ("accountability-capsule.json", 0, "ok"),
        ("invalid-tampered-context-capsule.json", 1, "defective"),
        ("invalid-dangling-ref-capsule.json", 1, "defective"),
        ("threaded-capsule.json", 2, "empty"),
    ],
)
def test_context_verify_exit_codes(tmp_path: Path, fixture: str, code: int, status: str) -> None:
    cap = _capsule_dir(tmp_path, fixture)
    result = _run("context", "verify", "--capsule", str(cap), "--json")
    assert result.exit_code == code
    assert _json(result)["status"] == status
    text = _run("context", "verify", "--capsule", str(cap))
    assert text.exit_code == code
    assert f"Status: {status}" in text.stdout


def test_context_verify_prints_defects(tmp_path: Path) -> None:
    def bad(d: dict[str, Any]) -> None:
        d["facets"]["conversation"]["decision_context"].append({"turn_ref": "t1"})

    cap = _capsule_dir(tmp_path, "accountability-capsule.json", bad)
    result = _run("context", "verify", "--capsule", str(cap))
    assert result.exit_code == 1
    assert "DEFECT decision_context entry 1" in _squash(result.stdout)


# ── override list ─────────────────────────────────────────────────────────


def test_override_list(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "accountability-capsule.json")
    result = _run("override", "list", "--capsule", str(cap), "--json")
    assert result.exit_code == 0
    rows = _json(result)["overrides"]
    assert rows[0]["turn_resolves"] is True and rows[0]["reason"] == "scope_too_broad"
    text = _run("override", "list", "--capsule", str(cap))
    assert "Overrides: 1" in text.stdout


def test_override_list_dangling_and_defect(tmp_path: Path) -> None:
    def bad(d: dict[str, Any]) -> None:
        d["facets"]["conversation"]["override"] = "oops"

    cap = _capsule_dir(tmp_path, "invalid-dangling-ref-capsule.json")
    result = _run("override", "list", "--capsule", str(cap))
    assert result.exit_code == 1
    assert "DANGLING" in result.stdout
    (tmp_path / "cap").rename(tmp_path / "old")
    cap = _capsule_dir(tmp_path, "accountability-capsule.json", bad)
    result = _run("override", "list", "--capsule", str(cap))
    assert result.exit_code == 1
    assert "DEFECT override list" in _squash(result.stdout)


def test_override_list_without_conversation_facet(tmp_path: Path) -> None:
    def orphan(d: dict[str, Any]) -> None:
        d["facets"] = {"conversation": {"override": []}}

    cap = _capsule_dir(tmp_path, "valid-text-only-capsule.json", orphan)
    result = _run("override", "list", "--capsule", str(cap), "--json")
    assert result.exit_code == 0
    assert _json(result)["overrides"] == []


# ── rationale show ────────────────────────────────────────────────────────


def test_rationale_show(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "accountability-capsule.json")
    result = _run("rationale", "show", "--capsule", str(cap), "--turn", "t1", "--json")
    assert result.exit_code == 0
    assert _json(result)["rationales"][0]["model_ref"] == "acme/triager-v3"
    text = _run("rationale", "show", "--capsule", str(cap), "--turn", "t1")
    assert "acme/triager-v3" in text.stdout


def test_rationale_show_missing_and_dangling(tmp_path: Path) -> None:
    def bad(d: dict[str, Any]) -> None:
        d["facets"]["conversation"]["rationale"].append({"turn_ref": "t1"})

    cap = _capsule_dir(tmp_path, "accountability-capsule.json", bad)
    result = _run("rationale", "show", "--capsule", str(cap), "--turn", "t9")
    assert result.exit_code == 1
    out = _squash(result.stdout)
    assert "Dangling turn_ref: t9" in out and "No rationale recorded" in out
    assert "DEFECT rationale entry 1" in out


# ── load failures (fail-closed) ───────────────────────────────────────────


def test_unknown_capsule(tmp_path: Path) -> None:
    result = _run("context", "verify", "--capsule", str(tmp_path / "nope"))
    assert result.exit_code == 1


def test_non_mapping_manifest(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("- a\n- b\n")
    assert _run("thread", "show", "--capsule", str(cap)).exit_code == 1


def test_unparseable_manifest(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("a: [unclosed\n")
    assert _run("thread", "show", "--capsule", str(cap)).exit_code == 1


def test_oversize_manifest_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    monkeypatch.setattr(hitl_cli, "MAX_MANIFEST_BYTES", 10)
    result = _run("thread", "show", "--capsule", str(cap))
    assert result.exit_code == 1


def test_malformed_conversation_facet(tmp_path: Path) -> None:
    def bad(d: dict[str, Any]) -> None:
        d["facets"]["conversation"]["turns"][0]["author"] = "alice"

    cap = _capsule_dir(tmp_path, "threaded-capsule.json", bad)
    result = _run("thread", "show", "--capsule", str(cap))
    assert result.exit_code == 1


def test_commands_are_read_only(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "accountability-capsule.json")
    before = {p.name: p.read_bytes() for p in cap.iterdir()}
    for args in (
        ["thread", "show"],
        ["context", "verify"],
        ["override", "list"],
    ):
        _run(*args, "--capsule", str(cap))
    _run("context", "show", "--capsule", str(cap), "--turn", "t2")
    _run("rationale", "show", "--capsule", str(cap), "--turn", "t1")
    assert {p.name: p.read_bytes() for p in cap.iterdir()} == before
