"""Observation log levels (ADR-0127, observation-log-levels-v0).

Covers: the severity vocabulary (normalization, strict write-time domain,
most-severe-wins resolution with provenance), write-time rejection in
``CapsuleWriter``, absence-preserved recording, the graduated schema fields on
both copies of the tool-call/model-call schemas (valid levels, absent fields,
out-of-domain rejection), the OTLP span-status → ``log_level`` inbound mapping,
and the ADR-0129 query-filter roundtrip over a capsule written through the
real write path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.log_level import (
    LOG_LEVEL_SOURCES,
    LOG_LEVELS,
    InvalidLogLevelError,
    log_level_from_span_status,
    most_severe,
    normalize_log_level,
    resolve_log_level,
    validate_log_level,
    validate_severity_fields,
)

REPO_ROOT = Path(__file__).parents[2]
SCHEMA_DIRS = (
    REPO_ROOT / "schemas",
    REPO_ROOT / "src" / "novafabric" / "schemas",
)


# ---------------------------------------------------------------------------
# Vocabulary — normalization and strict validation
# ---------------------------------------------------------------------------


class TestNormalizeLogLevel:
    @pytest.mark.parametrize("level", LOG_LEVELS)
    def test_canonical_values_pass_through(self, level: str) -> None:
        assert normalize_log_level(level) == level

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("WARNING", "warn"),
            ("warning", "warn"),
            ("Warn", "warn"),
            ("CRITICAL", "error"),
            ("FATAL", "error"),
            ("fatal", "error"),
            ("TRACE", "debug"),
            ("INFO", "info"),
            ("ERROR", "error"),
            ("DEBUG", "debug"),
        ],
    )
    def test_framework_names_normalize(self, raw: str, expected: str) -> None:
        assert normalize_log_level(raw) == expected

    @pytest.mark.parametrize("raw", ["loud", "", "  ", "verbose", "2", "warn "])
    def test_unknown_names_raise(self, raw: str) -> None:
        with pytest.raises(InvalidLogLevelError):
            normalize_log_level(raw)

    def test_non_string_raises(self) -> None:
        with pytest.raises(InvalidLogLevelError):
            normalize_log_level(2)  # type: ignore[arg-type]


class TestValidateLogLevel:
    @pytest.mark.parametrize("level", LOG_LEVELS)
    def test_canonical_values_pass(self, level: str) -> None:
        assert validate_log_level(level) == level

    @pytest.mark.parametrize("raw", ["WARNING", "warning", "fatal", "Info", "", 2, None])
    def test_everything_else_rejected(self, raw: Any) -> None:
        # Strict domain: producers normalize BEFORE writing; the stored value
        # is exactly one of the four lower-case levels (spec §Enum domain).
        with pytest.raises(InvalidLogLevelError):
            validate_log_level(raw)


class TestSeverityRank:
    def test_ascending_order(self) -> None:
        from novafabric.capture.log_level import severity_rank

        ranks = [severity_rank(level) for level in LOG_LEVELS]
        assert ranks == sorted(ranks)
        assert severity_rank("debug") < severity_rank("error")


class TestMostSevere:
    def test_orders_by_severity(self) -> None:
        assert most_severe("debug", "error", "warn") == "error"
        assert most_severe("info", "debug") == "info"
        assert most_severe("warn") == "warn"

    def test_no_levels_is_none(self) -> None:
        assert most_severe() is None
        assert most_severe(None, None) is None

    def test_none_entries_skipped(self) -> None:
        assert most_severe(None, "warn", None) == "warn"


class TestSpanStatusMapping:
    def test_error_span_maps_to_error(self) -> None:
        assert log_level_from_span_status("ERROR") == "error"

    def test_ok_span_maps_to_info(self) -> None:
        assert log_level_from_span_status("OK") == "info"

    def test_unset_span_sets_nothing(self) -> None:
        assert log_level_from_span_status("UNSET") is None

    def test_unknown_status_sets_nothing(self) -> None:
        assert log_level_from_span_status("weird") is None


class TestResolveLogLevel:
    def test_single_source(self) -> None:
        resolved = resolve_log_level(user="warn")
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("warn", "user")

    def test_most_severe_source_wins(self) -> None:
        # framework says info, span status is ERROR → error, span-status wins.
        resolved = resolve_log_level(framework="info", span_status="ERROR")
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("error", "span-status")

    def test_tie_broken_by_priority_order(self) -> None:
        # Equal severity: framework > span-status > adapter > user (spec §Provenance).
        resolved = resolve_log_level(framework="warn", adapter="warn", user="warn")
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("warn", "framework")

    def test_framework_names_are_normalized(self) -> None:
        resolved = resolve_log_level(framework="WARNING")
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("warn", "framework")

    def test_nothing_supplied_is_none(self) -> None:
        assert resolve_log_level() is None
        assert resolve_log_level(span_status="UNSET") is None

    def test_invalid_explicit_value_raises(self) -> None:
        with pytest.raises(InvalidLogLevelError):
            resolve_log_level(user="loud")


# ---------------------------------------------------------------------------
# Write path — CapsuleWriter records verbatim, rejects out-of-domain at write
# ---------------------------------------------------------------------------


def _open_writer(tmp_path: Path) -> CapsuleWriter:
    writer = CapsuleWriter(run_id="01HXAY7M9SM4YZ2K7N9DPBYK2W", base_dir=tmp_path)
    writer.open()
    return writer


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestCapsuleWriterSeverityFields:
    def test_level_recorded_verbatim_on_model_call(self, tmp_path: Path) -> None:
        writer = _open_writer(tmp_path)
        writer.append_model_call(
            {"model_call_id": "x", "log_level": "warn",
             "status_message": "retry 3/3 exhausted", "log_level_source": "adapter"}
        )
        [record] = _read_jsonl(writer.capsule_dir / "model-calls.jsonl")
        assert record["log_level"] == "warn"
        assert record["status_message"] == "retry 3/3 exhausted"
        assert record["log_level_source"] == "adapter"

    def test_level_recorded_verbatim_on_tool_call(self, tmp_path: Path) -> None:
        writer = _open_writer(tmp_path)
        writer.append_tool_call({"tool_call_id": "x", "log_level": "error"})
        [record] = _read_jsonl(writer.capsule_dir / "tool-calls.jsonl")
        assert record["log_level"] == "error"

    def test_absent_fields_stay_absent(self, tmp_path: Path) -> None:
        # Absence is preserved: the writer never back-fills log_level (spec
        # §Default & absence) and a record without the fields is unchanged.
        writer = _open_writer(tmp_path)
        writer.append_model_call({"model_call_id": "x", "status": "success"})
        [record] = _read_jsonl(writer.capsule_dir / "model-calls.jsonl")
        assert record == {"model_call_id": "x", "status": "success"}

    @pytest.mark.parametrize("bad", ["WARNING", "warning", "fatal", "trace", 2, ""])
    def test_invalid_level_rejected_at_write(self, tmp_path: Path, bad: Any) -> None:
        writer = _open_writer(tmp_path)
        with pytest.raises(InvalidLogLevelError):
            writer.append_model_call({"model_call_id": "x", "log_level": bad})
        with pytest.raises(InvalidLogLevelError):
            writer.append_tool_call({"tool_call_id": "x", "log_level": bad})
        # Nothing was written on rejection.
        assert _read_jsonl(writer.capsule_dir / "model-calls.jsonl") == []
        assert _read_jsonl(writer.capsule_dir / "tool-calls.jsonl") == []

    def test_invalid_source_rejected_at_write(self, tmp_path: Path) -> None:
        writer = _open_writer(tmp_path)
        with pytest.raises(InvalidLogLevelError):
            writer.append_tool_call(
                {"tool_call_id": "x", "log_level": "warn", "log_level_source": "guess"}
            )

    def test_non_string_status_message_rejected_at_write(self, tmp_path: Path) -> None:
        writer = _open_writer(tmp_path)
        with pytest.raises(InvalidLogLevelError):
            writer.append_model_call({"model_call_id": "x", "status_message": 42})

    def test_null_status_message_accepted(self, tmp_path: Path) -> None:
        writer = _open_writer(tmp_path)
        writer.append_model_call({"model_call_id": "x", "status_message": None})
        [record] = _read_jsonl(writer.capsule_dir / "model-calls.jsonl")
        assert record["status_message"] is None


class TestValidateSeverityFields:
    def test_record_without_fields_passes(self) -> None:
        validate_severity_fields({"status": "success"})  # no raise

    def test_source_alone_is_valid(self) -> None:
        for source in LOG_LEVEL_SOURCES:
            validate_severity_fields({"log_level_source": source})


# ---------------------------------------------------------------------------
# Schemas — additive optional fields on both copies of both record schemas
# ---------------------------------------------------------------------------


def _tool_call_record() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "tool_call_id": "01HXAY7M9SM4YZ2K7N9DPBYK2W",
        "parent_span_id": "9b2d04dac8e91f3a",
        "started_at": "2026-07-12T10:23:01.500000Z",
        "finished_at": "2026-07-12T10:23:01.980000Z",
        "duration_ms": 480,
        "tool_name": "web_search",
        "tool_version": "1.2.0",
        "tool_provider": "https://api.example.com",
        "transport": "http",
        "mutates": False,
        "mutation_class": "read-only",
        "arguments": {"query": "otel severity"},
        "arguments_schema_ref": None,
        "result": {"results": []},
        "result_schema_ref": None,
        "status": "success",
        "agent_call_id": None,
    }


def _model_call_record() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "semconv_version": "1.30.0",
        "model_call_id": "01HXAY7M6FN9TQGE0V0M7PAY1Q",
        "parent_span_id": "9b2d04dac8e91f3a",
        "started_at": "2026-07-12T10:23:00.100000Z",
        "finished_at": "2026-07-12T10:23:00.900000Z",
        "duration_ms": 800,
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.response.model": "gpt-4o",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.messages": [{"role": "user", "content": "hi"}],
        "gen_ai.response.choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 5,
        "status": "success",
    }


_RECORD_FACTORIES = {
    "tool-call.schema.json": _tool_call_record,
    "model-call.schema.json": _model_call_record,
}


def _schema_cases() -> list[Any]:
    return [
        pytest.param(schema_dir / name, factory, id=f"{schema_dir.name}-{name}")
        for schema_dir in SCHEMA_DIRS
        for name, factory in _RECORD_FACTORIES.items()
    ]


@pytest.mark.parametrize(("schema_path", "record_factory"), _schema_cases())
class TestSchemaLogLevelFields:
    def _validator(self, schema_path: Path) -> jsonschema.Draft202012Validator:
        schema = json.loads(schema_path.read_text())
        return jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )

    def test_record_without_fields_still_validates(
        self, schema_path: Path, record_factory: Any
    ) -> None:
        # Old capsules stay valid: the fields are never required.
        self._validator(schema_path).validate(record_factory())

    @pytest.mark.parametrize("level", LOG_LEVELS)
    def test_each_level_validates(
        self, schema_path: Path, record_factory: Any, level: str
    ) -> None:
        record = record_factory()
        record["log_level"] = level
        record["status_message"] = "one-line reason"
        record["log_level_source"] = "adapter"
        self._validator(schema_path).validate(record)

    def test_null_status_message_validates(
        self, schema_path: Path, record_factory: Any
    ) -> None:
        record = record_factory()
        record["log_level"] = "info"
        record["status_message"] = None
        self._validator(schema_path).validate(record)

    @pytest.mark.parametrize("bad", ["WARNING", "warning", "fatal", "trace", 2])
    def test_out_of_domain_level_rejected(
        self, schema_path: Path, record_factory: Any, bad: Any
    ) -> None:
        record = record_factory()
        record["log_level"] = bad
        with pytest.raises(jsonschema.ValidationError):
            self._validator(schema_path).validate(record)

    def test_bad_source_rejected(self, schema_path: Path, record_factory: Any) -> None:
        record = record_factory()
        record["log_level"] = "warn"
        record["log_level_source"] = "guess"
        with pytest.raises(jsonschema.ValidationError):
            self._validator(schema_path).validate(record)

    def test_non_string_status_message_rejected(
        self, schema_path: Path, record_factory: Any
    ) -> None:
        record = record_factory()
        record["status_message"] = 42
        with pytest.raises(jsonschema.ValidationError):
            self._validator(schema_path).validate(record)


# ---------------------------------------------------------------------------
# OTLP ingest — inbound span-status mapping (ADR-0127 D3 / spec §OTel mapping)
# ---------------------------------------------------------------------------


def _otlp_payload(*, error: bool, message: str = "") -> dict[str, Any]:
    status: dict[str, Any] = {"code": 2, "message": message} if error else {"code": 1}
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "chat gpt-4o",
                                "traceId": "0af7651916cd43dd8448eb211c80319c",
                                "spanId": "b7ad6b7169203331",
                                "startTimeUnixNano": "1720000000000000000",
                                "endTimeUnixNano": "1720000001000000000",
                                "status": status,
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {"stringValue": "chat"},
                                    },
                                    {
                                        "key": "gen_ai.request.model",
                                        "value": {"stringValue": "gpt-4o"},
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


class TestGenAIIngestSpanStatusMapping:
    def test_error_span_records_error_level(self) -> None:
        from novafabric.otel.genai_ingest import ingest_otlp_json

        result = ingest_otlp_json(_otlp_payload(error=True, message="529 overloaded"))
        [record] = result.model_calls
        assert record["log_level"] == "error"
        assert record["log_level_source"] == "span-status"
        assert record["status_message"] == "529 overloaded"

    def test_error_span_without_message_has_no_status_message(self) -> None:
        from novafabric.otel.genai_ingest import ingest_otlp_json

        result = ingest_otlp_json(_otlp_payload(error=True))
        [record] = result.model_calls
        assert record["log_level"] == "error"
        assert "status_message" not in record

    def test_ok_span_sets_no_level(self) -> None:
        # OK/UNSET spans do not set the field from the span alone (spec table).
        from novafabric.otel.genai_ingest import ingest_otlp_json

        result = ingest_otlp_json(_otlp_payload(error=False))
        [record] = result.model_calls
        assert "log_level" not in record
        assert "log_level_source" not in record


# ---------------------------------------------------------------------------
# Query roundtrip — the ADR-0129 filter matches the real recorded field
# ---------------------------------------------------------------------------


class TestQueryFilterRoundtrip:
    def test_min_level_filter_matches_written_capsule(self, tmp_path: Path) -> None:
        import yaml

        from novafabric.query import build_plan, run_query

        writer = _open_writer(tmp_path)
        writer.append_model_call(
            {"gen_ai.request.model": "m-warn", "duration_ms": 10, "log_level": "warn"}
        )
        writer.append_model_call(
            {"gen_ai.request.model": "m-plain", "duration_ms": 10}  # absent ⇒ info
        )
        writer.write_text(
            "capsule.yaml",
            yaml.dump(
                {
                    "schema_version": "0.1.0",
                    "run_id": "01HXAY7M9SM4YZ2K7N9DPBYK2W",
                    "created_at": "2026-07-12T10:00:00Z",
                    "status": "success",
                }
            ),
        )

        plan = build_plan(select="count()", where="log_level >= warn", group_by=["model"])
        result = run_query(plan, tmp_path, engine="sqlite")
        assert [row["model"] for row in result["rows"]] == ["m-warn"]

        # And the absent-level record is still visible as info.
        plan_all = build_plan(select="count()", where="log_level = info")
        result_all = run_query(plan_all, tmp_path, engine="sqlite")
        assert result_all["rows"] == [{"count()": 1}]
