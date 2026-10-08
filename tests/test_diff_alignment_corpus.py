"""Alignment corpus for ``nova diff`` (the private design/spec/diff-report-v1.md §alignment).

Before this, model calls paired only on ``parent_span_id``. Every capture gets a
fresh root span, so two separate captures NEVER paired: every call showed as one
"added" plus one "removed", and ``--assert-no-regressions`` (which counted only
"changed") passed on a pure model-behaviour change. Each case states the pairing
the spec intends: span match first, then sequence position, with identical
requests used as anchors so an inserted or deleted call does not shift the rest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.diff._align import align_model_calls, align_tool_calls


def _mc(prompt: str, *, model: str = "gpt-4o", span: str = "root", cid: str = "") -> dict:
    return {
        "model_call_id": cid or f"{span}:{prompt}",
        "parent_span_id": span,
        "gen_ai.system": "openai",
        "gen_ai.request.model": model,
        "gen_ai.request.messages": [{"role": "user", "content": prompt}],
    }


def _shape(pairs: list[tuple[Any, Any]]) -> list[tuple[str | None, str | None]]:
    def key(c: dict | None) -> str | None:
        if c is None:
            return None
        return c["gen_ai.request.messages"][0]["content"] + (
            "" if c["gen_ai.request.model"] == "gpt-4o" else f"@{c['gen_ai.request.model']}"
        )

    return [(key(a), key(b)) for a, b in pairs]


# Two separate captures: every call in A shares root span "sa", every call in B "sb".
def _a(*prompts: str, **kw: Any) -> list[dict]:
    return [_mc(p, span="sa", **kw) for p in prompts]


def _b(*prompts: str, **kw: Any) -> list[dict]:
    return [_mc(p, span="sb", **kw) for p in prompts]


class TestModelCallCorpus:
    def test_1_identical_three_calls(self) -> None:
        assert _shape(align_model_calls(_a("x", "y", "z"), _b("x", "y", "z"))) == [
            ("x", "x"), ("y", "y"), ("z", "z"),
        ]

    def test_2_prompt_changed_in_middle(self) -> None:
        assert _shape(align_model_calls(_a("x", "y", "z"), _b("x", "Y", "z"))) == [
            ("x", "x"), ("y", "Y"), ("z", "z"),
        ]

    def test_3_one_call_inserted(self) -> None:
        assert _shape(align_model_calls(_a("x", "y", "z"), _b("x", "w", "y", "z"))) == [
            ("x", "x"), (None, "w"), ("y", "y"), ("z", "z"),
        ]

    def test_4_one_call_deleted(self) -> None:
        assert _shape(align_model_calls(_a("x", "y", "z"), _b("x", "z"))) == [
            ("x", "x"), ("y", None), ("z", "z"),
        ]

    def test_5_model_changed(self) -> None:
        a = _a("x")
        b = [_mc("x", model="gpt-4o-mini", span="sb")]
        assert _shape(align_model_calls(a, b)) == [("x", "x@gpt-4o-mini")]

    def test_6_provider_changed(self) -> None:
        a = _a("x")
        b = _b("x")
        b[0]["gen_ai.system"] = "anthropic"
        pairs = align_model_calls(a, b)
        assert len(pairs) == 1 and pairs[0][0] is not None and pairs[0][1] is not None

    def test_7_repeated_calls_same_model(self) -> None:
        assert _shape(align_model_calls(_a("x", "x", "x"), _b("x", "x", "x"))) == [
            ("x", "x"), ("x", "x"), ("x", "x"),
        ]

    def test_8_identical_prompts_at_multiple_positions(self) -> None:
        assert _shape(align_model_calls(_a("x", "y", "x"), _b("x", "y", "x", "x"))) == [
            ("x", "x"), ("y", "y"), ("x", "x"), (None, "x"),
        ]

    def test_9_branch_changes_ordering(self) -> None:
        # Reordering is a change: one call keeps its anchor, the other moves.
        pairs = align_model_calls(_a("x", "y"), _b("y", "x"))
        shape = _shape(pairs)
        assert sum(1 for a, b in shape if a is not None and b is not None) >= 1
        assert {a for a, _ in shape if a} == {"x", "y"}
        assert {b for _, b in shape if b} == {"x", "y"}

    def test_10_different_numbers_of_calls(self) -> None:
        assert _shape(align_model_calls(_a("x"), _b("p", "q"))) == [("x", "p"), (None, "q")]

    def test_unique_span_ids_still_match_exactly_first(self) -> None:
        a = [_mc("x", span="s1"), _mc("y", span="s2")]
        b = [_mc("y2", span="s2"), _mc("x2", span="s1")]
        pairs = align_model_calls(a, b)
        by_a = {x["parent_span_id"]: y["parent_span_id"] for x, y in pairs if x and y}
        assert by_a == {"s1": "s1", "s2": "s2"}


class TestToolCallCorpus:
    @staticmethod
    def _tc(name: str, cid: str, **args: Any) -> dict:
        return {"tool_call_id": cid, "tool_name": name, "arguments": args}

    def test_changed_arguments_pair_by_position(self) -> None:
        a = [self._tc("search", "a1", q="london")]
        b = [self._tc("search", "b1", q="paris")]
        pairs = align_tool_calls(a, b)
        assert [(x["tool_call_id"], y["tool_call_id"]) for x, y in pairs] == [("a1", "b1")]

    def test_different_tool_names_do_not_pair(self) -> None:
        a = [self._tc("search", "a1")]
        b = [self._tc("write", "b1")]
        pairs = align_tool_calls(a, b)
        assert (pairs[0][0], pairs[0][1]) != (a[0], b[0])
        assert len(pairs) == 2

    def test_inserted_tool_call_does_not_shift_the_rest(self) -> None:
        a = [self._tc("search", "a1", q="x"), self._tc("fetch", "a2", u="y")]
        b = [
            self._tc("search", "b1", q="x"),
            self._tc("search", "b2", q="extra"),
            self._tc("fetch", "b3", u="y"),
        ]
        ids = {
            (x["tool_call_id"] if x else None, y["tool_call_id"] if y else None)
            for x, y in align_tool_calls(a, b)
        }
        assert ids == {("a1", "b1"), (None, "b2"), ("a2", "b3")}

    def test_parallel_tool_calls_in_another_order_still_match(self) -> None:
        a = [self._tc("fetch", "a1", u="x"), self._tc("fetch", "a2", u="y")]
        b = [self._tc("fetch", "b1", u="y"), self._tc("fetch", "b2", u="x")]
        ids = {(x["tool_call_id"], y["tool_call_id"]) for x, y in align_tool_calls(a, b)}
        assert ids == {("a1", "b2"), ("a2", "b1")}

    def test_duplicate_calls_each_used_once(self) -> None:
        a = [self._tc("ls", "a1"), self._tc("ls", "a2")]
        b = [self._tc("ls", "b1")]
        pairs = align_tool_calls(a, b)
        assert [(x["tool_call_id"], y["tool_call_id"] if y else None) for x, y in pairs] == [
            ("a1", "b1"), ("a2", None),
        ]


# ── the gate: "exits 1 on any change" (spec :21) ─────────────────────────────


def _write_capsule(root: Path, run_id: str, calls: list[dict]) -> Path:
    d = root / run_id
    (d / "outputs").mkdir(parents=True)
    (d / "capsule.yaml").write_text(f"run_id: {run_id}\nstatus: success\n")
    (d / "model-calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
    (d / "tool-calls.jsonl").write_text("")
    (d / "env.lock").write_text("python: '3.12'\n")
    return d


def _resp(text: str) -> list[dict]:
    return [{"message": {"role": "assistant", "content": text}}]


@pytest.fixture
def two_captures(tmp_path: Path) -> tuple[Path, Path]:
    a = _a("plan the migration")
    b = _b("plan the migration")
    a[0]["gen_ai.response.choices"] = _resp("use blue/green")
    b[0]["gen_ai.response.choices"] = _resp("disable rate limiting")
    return _write_capsule(tmp_path, "run-a", a), _write_capsule(tmp_path, "run-b", b)


def test_model_only_change_across_two_captures_is_reported_as_changed(
    two_captures: tuple[Path, Path],
) -> None:
    from novafabric.diff._engine import DiffEngine

    report = DiffEngine().compare(*two_captures)
    assert report.added_count == 0 and report.removed_count == 0
    (pair,) = report.model_call_pairs
    assert pair["changed"] and pair["response_changed"] and not pair["request_changed"]


def test_gate_fails_on_model_only_change_across_two_captures(
    two_captures: tuple[Path, Path],
) -> None:
    from novafabric.cli.main import app

    result = CliRunner().invoke(
        app, ["diff", str(two_captures[0]), str(two_captures[1]), "--assert-no-regressions"]
    )
    assert result.exit_code == 1, result.output


def test_gate_fails_on_added_call_alone(tmp_path: Path) -> None:
    from novafabric.cli.main import app

    a = _write_capsule(tmp_path, "run-a", _a("x"))
    b = _write_capsule(tmp_path, "run-b", _b("x", "y"))
    result = CliRunner().invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert result.exit_code == 1, result.output


def test_gate_passes_on_identical_captures(tmp_path: Path) -> None:
    from novafabric.cli.main import app

    a = _write_capsule(tmp_path, "run-a", _a("x", "y"))
    b = _write_capsule(tmp_path, "run-b", _b("x", "y"))
    result = CliRunner().invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert result.exit_code == 0, result.output


def test_model_calls_section_counts_only_model_calls(tmp_path: Path) -> None:
    from novafabric.diff._engine import DiffEngine

    a = _write_capsule(tmp_path, "run-a", _a("x"))
    b = _write_capsule(tmp_path, "run-b", _b("x"))
    (b / "tool-calls.jsonl").write_text(
        json.dumps({"tool_call_id": "t1", "tool_name": "search", "arguments": {}}) + "\n"
    )
    sections = DiffEngine().compare(a, b).as_dict()["sections"]
    assert sections["model_calls"]["added"] == 0
    assert sections["tool_calls"]["added"] == 1


def test_asset_ref_diff_honours_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name@version path silently ignored --assert-no-regressions."""
    import novafabric.cli.diff as diff_cli
    from novafabric.cli.main import app

    specs = {"1": {"model": "a"}, "2": {"model": "b"}}
    monkeypatch.setattr(
        diff_cli,
        "get_asset",
        lambda name, version: {"spec_json": json.dumps(specs[version])},
        raising=False,
    )
    result = CliRunner().invoke(app, ["diff", "agent@1", "agent@2", "--assert-no-regressions"])
    assert result.exit_code == 1, result.output
    same = CliRunner().invoke(app, ["diff", "agent@1", "agent@1", "--assert-no-regressions"])
    assert same.exit_code == 0, same.output

