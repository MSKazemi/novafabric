"""ADR-0127 P4 — OTel ``SeverityNumber`` projection on emitted GenAI spans.

Acceptance criteria:

- a model/tool record carrying a canonical ``log_level`` yields a span with
  ``novafabric.severity_number`` / ``novafabric.severity_text`` per the spec's
  mapping table (debug→5, info→9, warn→13, error→17);
- a record **without** ``log_level`` yields neither attribute — absence is never
  projected as ``INFO`` (no fabricated severity);
- an out-of-domain stored value (hand-edited capsule) is skipped fail-open, never
  normalized or raised;
- the root ``invoke_agent`` span never carries a severity;
- existing span attributes are unchanged (additive only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.capture.log_level import (
    LOG_LEVELS,
    InvalidLogLevelError,
    OtelSeverity,
    to_otel_severity,
)
from novafabric.otel.genai_emitter import (
    SEVERITY_NUMBER_ATTR,
    SEVERITY_TEXT_ATTR,
    emit_spans,
)

#: The spec's ``log_level`` ⇄ OTel mapping table (observation-log-levels-v0 §OTel mapping).
SPEC_TABLE: dict[str, tuple[str, int, range]] = {
    "debug": ("DEBUG", 5, range(5, 9)),
    "info": ("INFO", 9, range(9, 13)),
    "warn": ("WARN", 13, range(13, 17)),
    "error": ("ERROR", 17, range(17, 21)),
}


def _capsule(
    tmp_path: Path,
    *,
    model_calls: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> Path:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        yaml.dump({"run_id": "run-sev", "provider": "openai"}), encoding="utf-8"
    )
    for name, records in (
        ("model-calls.jsonl", model_calls),
        ("tool-calls.jsonl", tool_calls),
    ):
        if records is not None:
            (capsule / name).write_text(
                "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
            )
    return capsule


# ── the projection function (single source: capture/log_level.py) ───────────


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_to_otel_severity_matches_spec_table(level: str) -> None:
    text, number, allowed = SPEC_TABLE[level]
    severity = to_otel_severity(level)
    assert severity == OtelSeverity(text=text, number=number)
    assert severity.number in allowed


def test_projection_is_monotonic_in_severity() -> None:
    numbers = [to_otel_severity(level).number for level in LOG_LEVELS]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)


@pytest.mark.parametrize("bad", ["WARNING", "warning", "fatal", "trace", "", None, 13])
def test_to_otel_severity_rejects_non_canonical(bad: Any) -> None:
    with pytest.raises(InvalidLogLevelError):
        to_otel_severity(bad)


# ── emitted spans ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_model_span_carries_severity(tmp_path: Path, level: str) -> None:
    cap = _capsule(
        tmp_path,
        model_calls=[{"gen_ai.request.model": "gpt-4o", "log_level": level}],
    )
    attrs = emit_spans(cap)[1]["attributes"]
    text, number, _ = SPEC_TABLE[level]
    assert attrs[SEVERITY_NUMBER_ATTR] == number
    assert attrs[SEVERITY_TEXT_ATTR] == text
    # Additive: the existing markers are untouched.
    assert attrs["novafabric.semconv_maturity"] == "stable"
    assert attrs["gen_ai.request.model"] == "gpt-4o"


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_tool_span_carries_severity(tmp_path: Path, level: str) -> None:
    cap = _capsule(
        tmp_path,
        tool_calls=[
            {
                "tool_name": "web_search",
                "log_level": level,
                "log_level_source": "adapter",
                "status_message": "empty result set",
            }
        ],
    )
    attrs = emit_spans(cap)[1]["attributes"]
    text, number, _ = SPEC_TABLE[level]
    assert attrs[SEVERITY_NUMBER_ATTR] == number
    assert attrs[SEVERITY_TEXT_ATTR] == text
    assert attrs["gen_ai.tool.name"] == "web_search"
    assert attrs["novafabric.semconv_maturity"] == "development"


def test_absent_level_emits_no_severity(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        model_calls=[{"gen_ai.request.model": "gpt-4o"}],
        tool_calls=[{"tool_name": "t"}],
    )
    for span in emit_spans(cap):
        assert SEVERITY_NUMBER_ATTR not in span["attributes"]
        assert SEVERITY_TEXT_ATTR not in span["attributes"]


def test_null_level_emits_no_severity(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, tool_calls=[{"tool_name": "t", "log_level": None}])
    assert SEVERITY_NUMBER_ATTR not in emit_spans(cap)[1]["attributes"]


@pytest.mark.parametrize("bad", ["WARNING", "fatal", 17, ["error"]])
def test_out_of_domain_level_is_skipped_fail_open(tmp_path: Path, bad: Any) -> None:
    cap = _capsule(
        tmp_path,
        model_calls=[{"gen_ai.request.model": "m", "log_level": bad}],
        tool_calls=[{"tool_name": "t", "log_level": bad}],
    )
    spans = emit_spans(cap)
    assert len(spans) == 3
    for span in spans:
        assert SEVERITY_NUMBER_ATTR not in span["attributes"]
        assert SEVERITY_TEXT_ATTR not in span["attributes"]


def test_root_span_never_carries_severity(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, tool_calls=[{"tool_name": "t", "log_level": "error"}])
    root = emit_spans(cap)[0]
    assert root["attributes"]["gen_ai.operation.name"] == "invoke_agent"
    assert SEVERITY_NUMBER_ATTR not in root["attributes"]


def test_mixed_records_project_independently(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        tool_calls=[
            {"tool_name": "a", "log_level": "warn"},
            {"tool_name": "b"},
            {"tool_name": "c", "log_level": "error"},
        ],
    )
    numbers = [s["attributes"].get(SEVERITY_NUMBER_ATTR) for s in emit_spans(cap)[1:]]
    assert numbers == [13, None, 17]


def test_attribute_names_follow_otel_naming_rules() -> None:
    # Vendor-namespaced (no registered span severity attribute exists), lower-case,
    # dot-separated namespace + snake_case leaf mirroring the OTLP log field names.
    for name, leaf in (
        (SEVERITY_NUMBER_ATTR, "severity_number"),
        (SEVERITY_TEXT_ATTR, "severity_text"),
    ):
        assert name == f"novafabric.{leaf}"
        assert name == name.lower()
