"""CapsuleView projection tests (ADR-0140 P1) — schema, projection, redaction."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from novafabric.viewer.view import (
    CAPSULE_VIEW_SCHEMA_VERSION,
    _int_or_none,
    _read_jsonl,
    build_capsule_view,
)
from viewer.conftest import REDACTION_MARKER, RUN_ID, SECRET_ARGUMENT_VALUE

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "capsule-view.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())


def _validate(view: dict) -> None:
    Draft202012Validator(_SCHEMA, format_checker=FormatChecker()).validate(view)


# ── success: golden capsule ───────────────────────────────────────────────────


def test_view_validates_against_graduated_schema(golden_capsule_dir: Path) -> None:
    result = build_capsule_view(golden_capsule_dir)
    _validate(result.view)
    assert result.warnings == []


def test_header_projection(golden_capsule_dir: Path) -> None:
    view = build_capsule_view(golden_capsule_dir).view
    assert view["schema_version"] == CAPSULE_VIEW_SCHEMA_VERSION
    assert view["generator"]["name"] == "novafabric"
    header = view["capsule"]
    assert header["run_id"] == RUN_ID
    assert header["capture_mode"] == "cli-wrapper"
    assert header["started_at"] == "2026-01-01T00:00:00.000000Z"
    assert header["finished_at"] == "2026-01-01T00:00:10.000000Z"
    assert header["agent_id"] == "kubernetes_sentinel"
    assert header["capsule_hash"].startswith("sha256:")


def test_model_call_rows(golden_capsule_dir: Path) -> None:
    rows = build_capsule_view(golden_capsule_dir).view["model_calls"]
    assert rows[0] == {
        "model_call_id": "01HXAY7M6FN9TQGE0V0M7PAY1Q",
        "model": "claude-x-20260101",  # response model preferred over request model
        "status": "success",
        "input_tokens": 812,
        "output_tokens": 344,
        "latency_ms": 1420,
    }
    # second call has no usage/latency — nullable fields stay null, never invented
    assert rows[1]["model"] == "claude-x"
    assert rows[1]["input_tokens"] is None
    assert rows[1]["latency_ms"] is None


def test_tool_call_rows(golden_capsule_dir: Path) -> None:
    rows = build_capsule_view(golden_capsule_dir).view["tool_calls"]
    assert rows[0] == {
        "tool_call_id": "01HXAY7M7QM4YZ2K7N9DPBYK2W",
        "tool_name": "web_search",
        "mutation_class": "read-only",
        "status": "success",
        "duration_ms": 178,
    }
    # latency_ms is the fallback when duration_ms is absent
    assert rows[1]["duration_ms"] == 100


def test_score_rows_numeric_and_non_numeric(golden_capsule_dir: Path) -> None:
    rows = build_capsule_view(golden_capsule_dir).view["scores"]
    assert rows[0] == {"suite": "gaia", "value": 0.71}
    assert rows[1] == {"suite": "task_pass", "value": None}  # boolean is not a number


def test_lineage_refs_projection(golden_capsule_dir: Path) -> None:
    refs = build_capsule_view(golden_capsule_dir).view["lineage_refs"]
    assert refs["nodes"] == [
        "dataset:sha256:1234",
        f"capsule:{RUN_ID}",
        "model:claude-x",
    ]
    assert refs["edges"][0] == {
        "from": "dataset:sha256:1234",
        "to": f"capsule:{RUN_ID}",
        "type": "consumed",
    }


def test_title_only_when_given(golden_capsule_dir: Path) -> None:
    assert "title" not in build_capsule_view(golden_capsule_dir).view
    view = build_capsule_view(golden_capsule_dir, title="Nightly run").view
    assert view["title"] == "Nightly run"
    _validate(view)


def test_notes_point_to_real_verification(golden_capsule_dir: Path) -> None:
    view = build_capsule_view(golden_capsule_dir).view
    assert "nova verify" in view["notes"]
    assert "not the signed Evidence Bundle" in view["notes"]


# ── redaction invariant (ADR-0140 D3 / ADR-0009) ─────────────────────────────


def test_projection_never_surfaces_arguments_or_results(golden_capsule_dir: Path) -> None:
    serialized = json.dumps(build_capsule_view(golden_capsule_dir).view)
    assert SECRET_ARGUMENT_VALUE not in serialized
    assert "weather" not in serialized  # even benign argument content is not projected


def test_redaction_marker_preserved_verbatim(golden_capsule_dir: Path) -> None:
    view = build_capsule_view(golden_capsule_dir).view
    assert view["tool_calls"][1]["tool_name"] == f"send_email {REDACTION_MARKER}"


# ── edge cases ────────────────────────────────────────────────────────────────


def test_empty_capsule_yields_empty_sections(empty_capsule_dir: Path) -> None:
    result = build_capsule_view(empty_capsule_dir)
    view = result.view
    assert view["model_calls"] == []
    assert view["tool_calls"] == []
    assert view["scores"] == []
    assert view["lineage_refs"] == {"nodes": [], "edges": []}
    assert "agent_id" not in view["capsule"]
    assert "capsule_hash" not in view["capsule"]
    _validate(view)


def test_malformed_section_skipped_with_warning(golden_capsule_dir: Path) -> None:
    (golden_capsule_dir / "model-calls.jsonl").write_text("{not json}\n")
    result = build_capsule_view(golden_capsule_dir)
    assert result.view["model_calls"] == []
    assert result.view["tool_calls"] != []  # other sections still rendered
    assert any("model-calls.jsonl" in w for w in result.warnings)
    assert "Skipped sections" in result.view["notes"]
    _validate(result.view)


def test_schema_rejects_added_field(golden_capsule_dir: Path) -> None:
    view = build_capsule_view(golden_capsule_dir).view
    view["invented"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        _validate(view)


def test_int_or_none_coercions() -> None:
    assert _int_or_none(7) == 7
    assert _int_or_none(7.6) == 8  # capsule-recorded float rounds to int
    assert _int_or_none(True) is None  # bools are not counts
    assert _int_or_none("12") is None  # strings are never coerced
    assert _int_or_none(None) is None


def test_read_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n\n{"b": 2}\n')
    assert _read_jsonl(p) == [{"a": 1}, {"b": 2}]


# ── failure cases ─────────────────────────────────────────────────────────────


def test_missing_capsule_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_capsule_view(tmp_path / "nonexistent")


def test_file_not_dir_raises(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(NotADirectoryError):
        build_capsule_view(f)


def test_missing_manifest_raises(tmp_path: Path) -> None:
    d = tmp_path / "capsule"
    d.mkdir()
    with pytest.raises(FileNotFoundError, match="capsule.yaml"):
        build_capsule_view(d)


def test_non_mapping_manifest_falls_back(tmp_path: Path) -> None:
    d = tmp_path / "capsule"
    d.mkdir()
    (d / "capsule.yaml").write_text(yaml.dump(["not", "a", "mapping"]))
    view = build_capsule_view(d).view
    assert view["capsule"]["run_id"] == "capsule"  # dir name fallback
    assert view["capsule"]["capture_mode"] == "unknown"
