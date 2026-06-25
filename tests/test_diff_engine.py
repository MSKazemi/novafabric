from __future__ import annotations

import json
from pathlib import Path

import yaml

from novafabric.diff._engine import DiffEngine


def _make_capsule(tmp_path: Path, name: str, run_id: str = "RUN001") -> Path:
    cap = tmp_path / name
    cap.mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "outputs").mkdir()
    manifest = {"schema_version": "0.1.0", "run_id": run_id, "status": "success"}
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "env.lock").write_text(yaml.dump({
        "python": {"version": "3.12.3", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    (cap / "model-calls.jsonl").write_text("")
    (cap / "tool-calls.jsonl").write_text("")
    (cap / "outputs" / "stdout.txt").write_text("hello\n")
    return cap


def _write_model_call(path: Path, span_id: str, content: str = "answer") -> None:
    record = {
        "model_call_id": f"MC-{span_id}",
        "parent_span_id": span_id,
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.response.model": "gpt-4o",
        "gen_ai.request.messages": [{"role": "user", "content": "q"}],
        "gen_ai.response.choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "gen_ai.usage.input_tokens": 5,
        "gen_ai.usage.output_tokens": 10,
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _write_tool_call(path: Path, tool_name: str, call_id: str, result: str = "ok") -> None:
    record = {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "arguments": {"key": "val"},
        "result": result,
        "mutation_class": "read-only",
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def test_identical_capsules_zero_changes(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    # Same content in both
    report = DiffEngine().compare(cap_a, cap_b)
    assert report.changed_count == 0
    assert report.added_count == 0
    assert report.removed_count == 0


def test_env_python_version_change_detected(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    (cap_b / "env.lock").write_text(yaml.dump({
        "python": {"version": "3.11.0", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    report = DiffEngine().compare(cap_a, cap_b)
    env_fields = [c["field"] for c in report.env_changes]
    assert "python.version" in env_fields


def test_added_model_call_in_b(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    _write_model_call(cap_b / "model-calls.jsonl", "span1")
    report = DiffEngine().compare(cap_a, cap_b)
    added = [p for p in report.model_call_pairs if p.get("added")]
    assert len(added) == 1


def test_changed_model_response_detected(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    _write_model_call(cap_a / "model-calls.jsonl", "span1", "answer A")
    _write_model_call(cap_b / "model-calls.jsonl", "span1", "answer B")
    report = DiffEngine().compare(cap_a, cap_b)
    changed = [p for p in report.model_call_pairs if p.get("changed")]
    assert len(changed) == 1
    assert changed[0]["response_changed"]


def test_tool_call_result_change_detected(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    _write_tool_call(cap_a / "tool-calls.jsonl", "search", "TC1", result="result A")
    _write_tool_call(cap_b / "tool-calls.jsonl", "search", "TC2", result="result B")
    report = DiffEngine().compare(cap_a, cap_b)
    changed = [p for p in report.tool_call_pairs if p.get("changed")]
    assert len(changed) == 1


def test_output_stdout_change(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    (cap_b / "outputs" / "stdout.txt").write_text("different output\n")
    report = DiffEngine().compare(cap_a, cap_b)
    output_paths = [c["path"] for c in report.output_changes]
    assert "outputs/stdout.txt" in output_paths


def test_report_as_dict_structure(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    report = DiffEngine().compare(cap_a, cap_b)
    d = report.as_dict()
    assert "summary" in d
    assert "sections" in d
    assert "environment" in d["sections"]
    assert "model_calls" in d["sections"]


def test_report_write(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    report = DiffEngine().compare(cap_a, cap_b)
    out = tmp_path / "diff.json"
    report.write(out)
    assert out.exists()
    data = json.loads(out.read_text())
    assert "run_a_id" in data


def test_format_text_no_changes(tmp_path: Path) -> None:
    from novafabric.diff._format import format_text
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    report = DiffEngine().compare(cap_a, cap_b)
    text = format_text(report)
    assert "No differences found" in text


def test_format_text_with_changes(tmp_path: Path) -> None:
    from novafabric.diff._format import format_text
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    _write_model_call(cap_a / "model-calls.jsonl", "span1", "answer A")
    _write_model_call(cap_b / "model-calls.jsonl", "span1", "answer B")
    _write_tool_call(cap_a / "tool-calls.jsonl", "search", "TC1", result="old")
    _write_tool_call(cap_b / "tool-calls.jsonl", "other_tool", "TC2", result="new")
    (cap_b / "outputs" / "stdout.txt").write_text("changed\n")
    (cap_b / "env.lock").write_text(
        __import__("yaml").dump({
            "python": {"version": "3.11.0", "interpreter": "cpython"},
            "host": {"os": "linux", "arch": "x86_64"},
        })
    )
    report = DiffEngine().compare(cap_a, cap_b)
    text = format_text(report)
    assert "changed=" in text


def test_format_github_annotations_with_changes(tmp_path: Path) -> None:
    from novafabric.diff._format import format_github_annotations
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    _write_model_call(cap_a / "model-calls.jsonl", "span1", "A")
    _write_model_call(cap_b / "model-calls.jsonl", "span1", "B")
    _write_tool_call(cap_a / "tool-calls.jsonl", "call_x", "T1", result="old")
    (cap_b / "outputs" / "stdout.txt").write_text("changed\n")
    report = DiffEngine().compare(cap_a, cap_b)
    ann = format_github_annotations(report)
    assert "::" in ann


def test_format_github_annotations_no_changes(tmp_path: Path) -> None:
    from novafabric.diff._format import format_github_annotations
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    report = DiffEngine().compare(cap_a, cap_b)
    ann = format_github_annotations(report)
    assert "::notice" in ann


def test_format_json_is_valid_json(tmp_path: Path) -> None:
    from novafabric.diff._format import format_json
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    report = DiffEngine().compare(cap_a, cap_b)
    data = json.loads(format_json(report))
    assert "run_a_id" in data


def test_removed_model_call(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    _write_model_call(cap_a / "model-calls.jsonl", "span-only-in-a")
    report = DiffEngine().compare(cap_a, cap_b)
    removed = [p for p in report.model_call_pairs if p.get("removed")]
    assert len(removed) == 1


def test_removed_tool_call(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    _write_tool_call(cap_a / "tool-calls.jsonl", "only_in_a", "T1")
    report = DiffEngine().compare(cap_a, cap_b)
    removed = [p for p in report.tool_call_pairs if p.get("removed")]
    assert len(removed) == 1


def test_as_dict_tool_calls_emits_counts(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    # changed: same name+args but different result
    _write_tool_call(cap_a / "tool-calls.jsonl", "search", "T1", result="old")
    _write_tool_call(cap_b / "tool-calls.jsonl", "search", "T1", result="new")
    # added: only in B
    _write_tool_call(cap_b / "tool-calls.jsonl", "extra_tool", "T2", result="x")
    # removed: only in A
    _write_tool_call(cap_a / "tool-calls.jsonl", "gone_tool", "T3", result="y")
    report = DiffEngine().compare(cap_a, cap_b)
    tc = report.as_dict()["sections"]["tool_calls"]
    assert "changed" in tc
    assert "added" in tc
    assert "removed" in tc
    assert tc["changed"] == 1
    assert tc["added"] == 1
    assert tc["removed"] == 1
    assert tc["aligned"] == 1  # "search" pair exists in both A and B (changed, not added/removed)


def test_output_arbitrary_file_change_detected(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    (cap_a / "outputs" / "decision.json").write_text('{"action":"disable"}')
    (cap_b / "outputs" / "decision.json").write_text('{"action":"reduce"}')
    report = DiffEngine().compare(cap_a, cap_b)
    output_paths = [c["path"] for c in report.output_changes]
    assert "outputs/decision.json" in output_paths


def test_output_file_added_to_b_detected(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    (cap_b / "outputs" / "new_artifact.json").write_text('{"x":1}')
    report = DiffEngine().compare(cap_a, cap_b)
    output_paths = [c["path"] for c in report.output_changes]
    assert "outputs/new_artifact.json" in output_paths


def test_output_file_missing_in_b_detected(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNX")
    cap_b = _make_capsule(tmp_path, "b", "RUNY")
    (cap_a / "outputs" / "only_in_a.txt").write_text("gone")
    report = DiffEngine().compare(cap_a, cap_b)
    output_paths = [c["path"] for c in report.output_changes]
    assert "outputs/only_in_a.txt" in output_paths
