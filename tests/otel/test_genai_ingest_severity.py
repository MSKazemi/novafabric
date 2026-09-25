"""ADR-0127 P4 (inbound half) — OTel ``SeverityNumber`` consumed on OTLP ingest.

Acceptance criteria:

- ``from_otel_severity`` maps every OTel ``SeverityNumber`` 1–24 onto the spec
  table (TRACE/DEBUG→debug, INFO→info, WARN→warn, ERROR/FATAL→error) and is the
  inverse of ``to_otel_severity`` on each canonical number; ``0``, out-of-range
  and non-``int`` input yields ``None`` — never a guess, never a raise;
- a capsule emitted with ``--emit-otel-genai`` and re-ingested keeps each of the
  four levels on both model and tool records (export→import round trip), also
  through the sealed capsule written by ``write_ingest_capsule``;
- a span event's log-record ``severityNumber``/``severityText`` (int or
  protobuf-JSON enum name) and severity attributes are consumed; the most severe
  of all sources wins;
- precedence vs span status: most severe wins; span status keeps a tie; a
  severity-decided level records ``log_level_source: adapter``;
- malformed severity (out of range, numeric string, unknown text, bad event
  entries) decides nothing and stays visible under ``otlp.unmapped``; a
  consumed pair is no longer reported as unmapped;
- event inspection is bounded (``MAX_SEVERITY_EVENTS``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.capture.log_level import (
    LOG_LEVEL_SOURCES,
    LOG_LEVELS,
    from_otel_severity,
    from_otel_severity_text,
    to_otel_severity,
)
from novafabric.otel.genai_emitter import (
    SEVERITY_NUMBER_ATTR,
    SEVERITY_TEXT_ATTR,
    emit_spans,
)
from novafabric.otel.genai_ingest import (
    MAX_SEVERITY_EVENTS,
    SEVERITY_LOG_LEVEL_SOURCE,
    ingest_otlp_json,
    parse_otlp_json,
    write_ingest_capsule,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "otlp"

#: The spec's inbound mapping table (observation-log-levels-v0 §OTel mapping).
SPEC_RANGES: tuple[tuple[range, str], ...] = (
    (range(1, 5), "debug"),  # TRACE
    (range(5, 9), "debug"),  # DEBUG
    (range(9, 13), "info"),  # INFO
    (range(13, 17), "warn"),  # WARN
    (range(17, 21), "error"),  # ERROR
    (range(21, 25), "error"),  # FATAL
)


def _payload(*spans: dict[str, Any]) -> dict[str, Any]:
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


def _any_value(value: Any) -> Any:
    """Wrap a test value as an OTLP JSON ``AnyValue`` (dicts are pre-wrapped)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return {"intValue": str(value)}  # OTLP JSON encodes int64 as a string
    return {"stringValue": value}


def _span(
    attrs: dict[str, Any] | None = None,
    *,
    error: bool = False,
    events: list[Any] | None = None,
    tool: bool = False,
) -> dict[str, Any]:
    base = {"gen_ai.tool.name": "t"} if tool else {"gen_ai.request.model": "m"}
    span: dict[str, Any] = {
        "name": "s",
        "traceId": "t" * 32,
        "spanId": "s" * 16,
        "attributes": [
            {"key": k, "value": _any_value(v)} for k, v in {**base, **(attrs or {})}.items()
        ],
    }
    if error:
        span["status"] = {"code": 2, "message": "boom"}
    if events is not None:
        span["events"] = events
    return span


def _one(payload: dict[str, Any], *, tool: bool = False) -> dict[str, Any]:
    result = ingest_otlp_json(payload)
    records = result.tool_calls if tool else result.model_calls
    assert len(records) == 1
    return records[0]


# ── from_otel_severity: the spec mapping table ──────────────────────────────


@pytest.mark.parametrize(
    ("number", "level"),
    [(n, level) for numbers, level in SPEC_RANGES for n in numbers],
)
def test_from_otel_severity_matches_spec_table(number: int, level: str) -> None:
    assert from_otel_severity(number) == level


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_from_otel_severity_inverts_projection(level: str) -> None:
    assert from_otel_severity(to_otel_severity(level).number) == level


@pytest.mark.parametrize(
    "bad", [0, -1, 25, 99, 2**63, True, False, 13.0, "13", "WARN", None, [13], {"n": 13}]
)
def test_from_otel_severity_rejects_without_guessing(bad: Any) -> None:
    assert from_otel_severity(bad) is None


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("DEBUG", "debug"),
        ("trace", "debug"),
        ("TRACE3", "debug"),
        ("INFO", "info"),
        ("info2", "info"),
        ("WARN", "warn"),
        ("Warning", "warn"),
        ("WARN4", "warn"),
        ("ERROR", "error"),
        ("FATAL", "error"),
        ("FATAL2", "error"),
        ("critical", "error"),
        (" warn ", "warn"),
    ],
)
def test_from_otel_severity_text(text: str, level: str) -> None:
    assert from_otel_severity_text(text) == level


@pytest.mark.parametrize(
    "bad", ["", "SEVERE", "WARN5", "WARN1", "notice", "x" * 64, None, 13, b"WARN"]
)
def test_from_otel_severity_text_rejects(bad: Any) -> None:
    assert from_otel_severity_text(bad) is None


def test_reused_source_is_in_the_closed_vocabulary() -> None:
    assert SEVERITY_LOG_LEVEL_SOURCE in LOG_LEVEL_SOURCES


# ── export → import round trip ──────────────────────────────────────────────


def _emitted_capsule(tmp_path: Path, level: str) -> Path:
    cap = tmp_path / f"cap-{level}"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(
        yaml.dump({"run_id": "run-sev-rt", "provider": "openai"}), encoding="utf-8"
    )
    (cap / "model-calls.jsonl").write_text(
        json.dumps({"gen_ai.request.model": "gpt-4o", "log_level": level}) + "\n",
        encoding="utf-8",
    )
    (cap / "tool-calls.jsonl").write_text(
        json.dumps({"tool_name": "web_search", "log_level": level}) + "\n",
        encoding="utf-8",
    )
    return cap


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_round_trip_preserves_level(tmp_path: Path, level: str) -> None:
    spans = emit_spans(_emitted_capsule(tmp_path, level))
    result = ingest_otlp_json(_payload(*spans))
    assert len(result.model_calls) == 1 and len(result.tool_calls) == 1
    for record in (result.model_calls[0], result.tool_calls[0]):
        assert record["log_level"] == level
        assert record["log_level_source"] == SEVERITY_LOG_LEVEL_SOURCE
        assert "otlp.unmapped" not in record
    # Consumed attributes are recognized, not reported as unknown.
    assert result.unmapped_keys == []


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_round_trip_through_sealed_capsule(tmp_path: Path, level: str) -> None:
    spans = emit_spans(_emitted_capsule(tmp_path, level))
    capsule = write_ingest_capsule(ingest_otlp_json(_payload(*spans)), tmp_path / "out")
    for name in ("model-calls.jsonl", "tool-calls.jsonl"):
        lines = (capsule / name).read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
        assert [r["log_level"] for r in records] == [level]
        assert [r["log_level_source"] for r in records] == ["adapter"]


def test_round_trip_absent_level_stays_absent(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.dump({"run_id": "r"}), encoding="utf-8")
    (cap / "model-calls.jsonl").write_text(
        json.dumps({"gen_ai.request.model": "m"}) + "\n", encoding="utf-8"
    )
    record = _one(_payload(*emit_spans(cap)))
    assert "log_level" not in record
    assert "log_level_source" not in record


# ── precedence vs span status ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("number", "error", "level", "source"),
    [
        (13, False, "warn", "adapter"),  # severity alone
        (5, False, "debug", "adapter"),  # a low level is still recorded
        (13, True, "error", "span-status"),  # span ERROR more severe
        (17, True, "error", "span-status"),  # tie → span status keeps it
        (21, True, "error", "span-status"),  # FATAL collapses to error → tie
        (None, True, "error", "span-status"),  # pre-P4 behaviour unchanged
    ],
)
def test_precedence(number: int | None, error: bool, level: str, source: str) -> None:
    attrs = {SEVERITY_NUMBER_ATTR: number} if number is not None else {}
    record = _one(_payload(_span(attrs, error=error)))
    assert record["log_level"] == level
    assert record["log_level_source"] == source
    if error:
        assert record["status"] == "error"
        assert record["status_message"] == "boom"
    else:
        assert record["status"] == "success"
        assert "status_message" not in record


def test_number_wins_over_disagreeing_text() -> None:
    record = _one(_payload(_span({SEVERITY_NUMBER_ATTR: 9, SEVERITY_TEXT_ATTR: "ERROR"})))
    assert record["log_level"] == "info"


def test_text_used_when_number_absent() -> None:
    record = _one(_payload(_span({SEVERITY_TEXT_ATTR: "WARNING"})))
    assert record["log_level"] == "warn"
    assert record["log_level_source"] == "adapter"


# ── span events / log-record fields ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("event", "level"),
    [
        ({"severityNumber": 14}, "warn"),
        ({"severityNumber": "SEVERITY_NUMBER_WARN"}, "warn"),
        ({"severityNumber": "SEVERITY_NUMBER_FATAL4"}, "error"),
        ({"severityNumber": "SEVERITY_NUMBER_TRACE"}, "debug"),
        ({"severityText": "ERROR"}, "error"),
        (
            {"attributes": [{"key": SEVERITY_NUMBER_ATTR, "value": {"intValue": "17"}}]},
            "error",
        ),
    ],
)
def test_span_event_severity(event: dict[str, Any], level: str) -> None:
    record = _one(_payload(_span(events=[{"name": "log", **event}])), tool=False)
    assert record["log_level"] == level
    assert record["log_level_source"] == "adapter"


def test_most_severe_across_attrs_and_events() -> None:
    span = _span(
        {SEVERITY_NUMBER_ATTR: 9},
        events=[{"severityNumber": 5}, {"severityNumber": 15}, {"severityNumber": 10}],
        tool=True,
    )
    record = _one(_payload(span), tool=True)
    assert record["log_level"] == "warn"


def test_event_scan_is_bounded() -> None:
    events: list[Any] = [{"severityNumber": 5}] * MAX_SEVERITY_EVENTS
    events.append({"severityNumber": 17})  # beyond the cap — not inspected
    record = _one(_payload(_span(events=events)))
    assert record["log_level"] == "debug"
    parsed = parse_otlp_json(_payload(_span(events=events)))
    assert len(parsed[0]["events"]) == MAX_SEVERITY_EVENTS


def test_parse_exposes_events_view() -> None:
    parsed = parse_otlp_json(_payload(_span(events=[{"name": "e", "severityText": "INFO"}])))
    assert parsed[0]["events"] == [{"attributes": {}, "severityText": "INFO"}]
    assert parse_otlp_json(_payload(_span()))[0]["events"] == []
    assert parse_otlp_json(_payload(_span(events="nope")))[0]["events"] == []  # type: ignore[arg-type]


# ── malformed input: ignored, never guessed, still visible ──────────────────


@pytest.mark.parametrize(
    "attrs",
    [
        {SEVERITY_NUMBER_ATTR: 0},
        {SEVERITY_NUMBER_ATTR: 25},
        {SEVERITY_NUMBER_ATTR: -3},
        {SEVERITY_NUMBER_ATTR: "13"},  # numeric string is not an int AnyValue
        {SEVERITY_NUMBER_ATTR: {"doubleValue": 13.0}},
        {SEVERITY_NUMBER_ATTR: {"boolValue": True}},
        {SEVERITY_NUMBER_ATTR: "SEVERITY_NUMBER_BOGUS"},
        {SEVERITY_NUMBER_ATTR: "SEVERITY_NUMBER_"},
        {SEVERITY_NUMBER_ATTR: 99, SEVERITY_TEXT_ATTR: "ERROR"},  # text can't rescue
        {SEVERITY_TEXT_ATTR: "SEVERE"},
    ],
)
def test_malformed_severity_is_ignored_and_preserved(attrs: dict[str, Any]) -> None:
    result = ingest_otlp_json(_payload(_span(attrs)))
    record = result.model_calls[0]
    assert "log_level" not in record
    assert "log_level_source" not in record
    assert set(attrs) <= set(record["otlp.unmapped"])
    assert set(attrs) <= set(result.unmapped_keys)


def test_malformed_severity_does_not_mask_span_status() -> None:
    record = _one(_payload(_span({SEVERITY_NUMBER_ATTR: 999}, error=True)))
    assert record["log_level"] == "error"
    assert record["log_level_source"] == "span-status"


def test_malformed_events_are_skipped() -> None:
    events: list[Any] = ["x", 3, None, {"severityNumber": 0}, {"severityNumber": [1]}]
    record = _one(_payload(_span(events=events)))
    assert "log_level" not in record


# ── golden fixtures ─────────────────────────────────────────────────────────


def test_golden_valid_fixture() -> None:
    payload = json.loads((FIXTURES / "genai-traces-severity-valid.json").read_text())
    result = ingest_otlp_json(payload)
    warn_call, error_call = result.model_calls
    assert (warn_call["log_level"], warn_call["log_level_source"]) == ("warn", "adapter")
    # ERROR span status beats a DEBUG severity attribute.
    assert (error_call["log_level"], error_call["log_level_source"]) == (
        "error",
        "span-status",
    )
    (tool_call,) = result.tool_calls
    # FATAL span event collapses to error.
    assert (tool_call["log_level"], tool_call["log_level_source"]) == ("error", "adapter")
    assert result.unmapped_keys == []


def test_golden_malformed_fixture() -> None:
    payload = json.loads((FIXTURES / "genai-traces-severity-malformed.json").read_text())
    result = ingest_otlp_json(payload)
    assert len(result.model_calls) == 3
    for record in result.model_calls:
        assert "log_level" not in record
        assert "otlp.unmapped" in record
    assert result.unmapped_keys == [SEVERITY_NUMBER_ATTR, SEVERITY_TEXT_ATTR]


# ── protobuf wire path (int64 AnyValue) ─────────────────────────────────────


def test_protobuf_severity_attribute_round_trips() -> None:
    pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    from novafabric.otel.genai_ingest import ingest_otlp_protobuf

    req = ExportTraceServiceRequest()
    sp = req.resource_spans.add().scope_spans.add().spans.add()
    sp.name = "chat"
    kv = sp.attributes.add()
    kv.key, kv.value.string_value = "gen_ai.request.model", "m"
    kv = sp.attributes.add()
    kv.key, kv.value.int_value = SEVERITY_NUMBER_ATTR, 13
    record = ingest_otlp_protobuf(req.SerializeToString()).model_calls[0]
    assert record["log_level"] == "warn"
    assert record["log_level_source"] == "adapter"
