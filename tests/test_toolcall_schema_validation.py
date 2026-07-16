"""ADR-0128 — tool-call schema validation (record-only enforcement pass).

Covers the validator core (verdict computation, local-only resolution,
bounded errors, sanitization), the capture wiring (CapsuleWriter attaches the
verdict; no-schema records byte-identical; never raises), and the replay-time
re-validation (drift surfaces on the replay result; exact hard-refuses).
"""
from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import pytest
import yaml

from novafabric.capture._ulid import new_ulid
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.schema_validation import (
    MAX_ERRORS,
    VALIDATOR_ID,
    annotate_tool_call,
    compute_verdict,
    revalidate_tool_calls,
    validate_capsule_tool_calls,
    write_back_verdicts,
)
from novafabric.replay._engine import ReplayEngine
from novafabric.replay._flags import ReplayFlags

ARGS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["query"],
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}

RESULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["price"],
                "properties": {"price": {"type": "number"}},
            },
        }
    },
}


def _write_schemas(capsule_dir: Path) -> None:
    ext = capsule_dir / "extensions" / "io.test"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "args.schema.json").write_text(json.dumps(ARGS_SCHEMA))
    (ext / "result.schema.json").write_text(json.dumps(RESULT_SCHEMA))


def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "0.1.0",
        "tool_call_id": new_ulid(),
        "parent_span_id": "0123456789abcdef",
        "started_at": "2026-07-15T10:00:00.000000Z",
        "finished_at": "2026-07-15T10:00:00.100000Z",
        "duration_ms": 100,
        "tool_name": "web_search",
        "tool_version": "unknown",
        "tool_provider": "shell://",
        "transport": "shell",
        "mutates": False,
        "mutation_class": "read-only",
        "arguments": {"query": "hello"},
        "arguments_schema_ref": "extensions/io.test/args.schema.json",
        "result": {"items": [{"price": 1.5}]},
        "result_schema_ref": "extensions/io.test/result.schema.json",
        "status": "success",
        "agent_call_id": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- core


def test_no_schema_ref_returns_none_and_record_unchanged(tmp_path: Path) -> None:
    record = _record(arguments_schema_ref=None, result_schema_ref=None)
    before = copy.deepcopy(record)
    assert compute_verdict(record, tmp_path) is None
    annotate_tool_call(record, tmp_path)
    assert record == before  # today's behavior, byte-identical


def test_valid_arguments_and_result(tmp_path: Path) -> None:
    _write_schemas(tmp_path)
    verdict = compute_verdict(_record(), tmp_path)
    assert verdict is not None
    assert verdict["arguments_valid"] is True
    assert verdict["result_valid"] is True
    assert verdict["errors"] == []
    assert verdict["validator"] == VALIDATOR_ID
    assert verdict["arguments_schema_ref"] == "extensions/io.test/args.schema.json"
    assert verdict["result_schema_ref"] == "extensions/io.test/result.schema.json"
    assert "checked_at" in verdict


def test_invalid_arguments_recorded_not_raised(tmp_path: Path) -> None:
    _write_schemas(tmp_path)
    record = _record(arguments={"query": 42})
    verdict = compute_verdict(record, tmp_path)  # must not raise
    assert verdict is not None
    assert verdict["arguments_valid"] is False
    assert verdict["result_valid"] is True
    [err] = [e for e in verdict["errors"] if e["target"] == "arguments"]
    assert err["keyword"] == "type"
    assert "query" in err["path"]


def test_invalid_result_recorded(tmp_path: Path) -> None:
    _write_schemas(tmp_path)
    record = _record(result={"items": [{"price": "free"}]})
    verdict = compute_verdict(record, tmp_path)
    assert verdict is not None
    assert verdict["result_valid"] is False
    [err] = verdict["errors"]
    assert err["target"] == "result"
    assert err["keyword"] == "type"
    assert "price" in err["path"]


def test_result_validation_skipped_when_tool_errored(tmp_path: Path) -> None:
    _write_schemas(tmp_path)
    record = _record(status="error", result=None)
    verdict = compute_verdict(record, tmp_path)
    assert verdict is not None
    assert verdict["arguments_valid"] is True
    assert verdict["result_valid"] is None  # skipped, not a failure
    assert verdict["errors"] == []


def test_null_result_with_schema_forbidding_null_is_violation(tmp_path: Path) -> None:
    _write_schemas(tmp_path)
    record = _record(result=None)  # status stays "success"
    verdict = compute_verdict(record, tmp_path)
    assert verdict is not None
    assert verdict["result_valid"] is False


def test_unresolved_missing_schema_file(tmp_path: Path) -> None:
    record = _record(
        arguments_schema_ref="extensions/io.test/missing.schema.json",
        result_schema_ref=None,
    )
    verdict = compute_verdict(record, tmp_path)
    assert verdict is not None
    assert verdict["arguments_valid"] is None
    [err] = verdict["errors"]
    assert err["keyword"] == "schema-unresolved"


def test_unparseable_schema_file_is_unresolved(tmp_path: Path) -> None:
    (tmp_path / "bad.schema.json").write_text("{not json")
    record = _record(arguments_schema_ref="bad.schema.json", result_schema_ref=None)
    verdict = compute_verdict(record, tmp_path)
    assert verdict is not None
    assert verdict["arguments_valid"] is None
    assert verdict["errors"][0]["keyword"] == "schema-unresolved"


def test_network_ref_never_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network access attempted during schema validation")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    record = _record(
        arguments_schema_ref="https://example.com/args.schema.json",
        result_schema_ref=None,
    )
    verdict = compute_verdict(record, tmp_path)
    assert verdict is not None
    assert verdict["arguments_valid"] is None
    [err] = verdict["errors"]
    assert err["keyword"] == "schema-unresolved"
    assert "offline" in err["message"]


def test_relative_ref_escaping_capsule_dir_is_unresolved(tmp_path: Path) -> None:
    outside = tmp_path / "outside.schema.json"
    outside.write_text(json.dumps(ARGS_SCHEMA))
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    record = _record(
        arguments_schema_ref="../outside.schema.json", result_schema_ref=None
    )
    verdict = compute_verdict(record, capsule)
    assert verdict is not None
    assert verdict["arguments_valid"] is None
    assert verdict["errors"][0]["keyword"] == "schema-unresolved"


def test_errors_capped_with_synthetic_truncated_entry(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "number"}}}}
    (tmp_path / "cap.schema.json").write_text(json.dumps(schema))
    record = _record(
        arguments={"items": ["bad"] * (MAX_ERRORS + 10)},
        arguments_schema_ref="cap.schema.json",
        result_schema_ref=None,
    )
    verdict = compute_verdict(record, tmp_path)
    assert verdict is not None
    assert verdict["arguments_valid"] is False
    assert len(verdict["errors"]) == MAX_ERRORS + 1
    assert verdict["errors"][-1]["keyword"] == "truncated"


def test_error_messages_are_secret_sanitized(tmp_path: Path) -> None:
    (tmp_path / "num.schema.json").write_text(json.dumps({"type": "number"}))
    secret = "sk-ant-" + "a1B2" * 8
    record = _record(
        arguments=secret,  # a string where a number is required
        arguments_schema_ref="num.schema.json",
        result_schema_ref=None,
    )
    verdict = compute_verdict(record, tmp_path)
    assert verdict is not None
    assert verdict["arguments_valid"] is False
    assert secret not in verdict["errors"][0]["message"]


@pytest.mark.parametrize(
    "schema_path",
    [
        Path(__file__).parents[1] / "schemas" / "tool-call.schema.json",
        Path(__file__).parents[1] / "src" / "novafabric" / "schemas"
        / "tool-call.schema.json",
    ],
    ids=["root-schema", "packaged-schema"],
)
def test_annotated_record_validates_against_tool_call_schema(
    tmp_path: Path, schema_path: Path
) -> None:
    _write_schemas(tmp_path)
    record = _record(
        schema_version="1.0.0"
        if "src" not in str(schema_path)
        else "0.1.0",
        transport="shell",
        result={"items": [{"price": "free"}]},  # violation → errors populated
    )
    annotate_tool_call(record, tmp_path)
    assert "schema_validation" in record
    tool_call_schema = json.loads(schema_path.read_text())
    jsonschema.validate(
        record, tool_call_schema, format_checker=jsonschema.FormatChecker()
    )


# ---------------------------------------------------------------- capture


def test_capsule_writer_attaches_verdict_when_refs_present(tmp_path: Path) -> None:
    writer = CapsuleWriter(run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX", base_dir=tmp_path)
    writer.open()
    _write_schemas(writer.capsule_dir)
    writer.append_tool_call(_record(arguments={"query": 42}))
    [line] = (writer.capsule_dir / "tool-calls.jsonl").read_text().splitlines()
    written = json.loads(line)
    assert written["schema_validation"]["arguments_valid"] is False
    assert written["schema_validation"]["result_valid"] is True


def test_capsule_writer_leaves_no_ref_records_unchanged(tmp_path: Path) -> None:
    writer = CapsuleWriter(run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX", base_dir=tmp_path)
    writer.open()
    record = _record(arguments_schema_ref=None, result_schema_ref=None)
    expected = copy.deepcopy(record)
    writer.append_tool_call(record)
    [line] = (writer.capsule_dir / "tool-calls.jsonl").read_text().splitlines()
    assert json.loads(line) == expected


def test_capsule_writer_never_raises_on_validator_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novafabric.capture.schema_validation as sv

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(sv, "annotate_tool_call", _boom)
    writer = CapsuleWriter(run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX", base_dir=tmp_path)
    writer.open()
    writer.append_tool_call(_record())  # must not raise into the workload
    assert (writer.capsule_dir / "tool-calls.jsonl").read_text().strip()


# ---------------------------------------------------------------- capsule report


def _capsule_with_tool_calls(
    tmp_path: Path, records: list[dict[str, Any]], run_id: str = "TESTRUN01"
) -> Path:
    cap = tmp_path / "capsules" / run_id
    cap.mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "outputs").mkdir()
    (cap / "capsule.yaml").write_text(yaml.dump({
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": "success",
        "command": ["python", "-c", "pass"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.2.0",
        "model_call_count": 0,
        "tool_call_count": len(records),
    }))
    (cap / "env.lock").write_text(yaml.dump({
        "schema_version": "0.1.0",
        "python": {"version": "3.12.3", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    (cap / "replay.yaml").write_text(yaml.dump({"schema_version": "0.1.0"}))
    (cap / "model-calls.jsonl").write_text("")
    (cap / "trace.jsonl").write_text("")
    (cap / "assets.jsonl").write_text("")
    (cap / "redaction-proof.json").write_text(json.dumps({"findings": []}))
    (cap / "tool-calls.jsonl").write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
    )
    _write_schemas(cap)
    return cap


def test_validate_capsule_tool_calls_report(tmp_path: Path) -> None:
    cap = _capsule_with_tool_calls(tmp_path, [
        _record(),  # clean
        _record(result={"items": [{"price": "free"}]}),  # violation
        _record(arguments_schema_ref=None, result_schema_ref=None),  # no schema
    ])
    report = validate_capsule_tool_calls(cap)
    assert report.total == 3
    assert report.no_schema == 1
    assert report.checked == 2
    assert report.arguments_checked == 2 and report.arguments_valid == 2
    assert report.result_checked == 2 and report.result_valid == 1
    assert len(report.violations) == 1
    assert report.payloads_checked == 4 and report.payloads_valid == 3


def test_write_back_verdicts_backfills_records(tmp_path: Path) -> None:
    cap = _capsule_with_tool_calls(tmp_path, [
        _record(),
        _record(arguments_schema_ref=None, result_schema_ref=None),
    ])
    annotated = write_back_verdicts(cap)
    assert annotated == 1
    lines = (cap / "tool-calls.jsonl").read_text().splitlines()
    first, second = (json.loads(line) for line in lines)
    assert first["schema_validation"]["arguments_valid"] is True
    assert "schema_validation" not in second  # no schema → unchanged


# ---------------------------------------------------------------- replay


def test_forensic_replay_surfaces_schema_drift(tmp_path: Path) -> None:
    cap = _capsule_with_tool_calls(
        tmp_path, [_record(result={"items": [{"price": "free"}]})]
    )
    base = tmp_path / "replays"
    result = ReplayEngine(
        capsule_dir=cap, flags=ReplayFlags(mode="forensic"), base_dir=base
    ).run()
    assert result.schema_drift is not None
    [finding] = result.schema_drift
    assert finding["result_valid"] is False
    assert finding["tool_name"] == "web_search"
    data = yaml.safe_load((base / result.replay_id / "replay_result.yaml").read_text())
    assert data["schema_drift"][0]["result_valid"] is False


def test_exact_replay_hard_refuses_on_schema_drift(tmp_path: Path) -> None:
    cap = _capsule_with_tool_calls(
        tmp_path, [_record(result={"items": [{"price": "free"}]})]
    )
    result = ReplayEngine(
        capsule_dir=cap,
        flags=ReplayFlags(mode="exact"),
        base_dir=tmp_path / "replays",
    ).run()
    assert result.exact_eligible is False
    assert any("schema drift" in r for r in result.exact_reasons or [])
    assert result.schema_drift is not None


def test_semantic_replay_records_drift_without_gating(tmp_path: Path) -> None:
    cap = _capsule_with_tool_calls(
        tmp_path, [_record(arguments={"query": 42})]
    )
    result = ReplayEngine(
        capsule_dir=cap,
        flags=ReplayFlags(mode="semantic"),
        base_dir=tmp_path / "replays",
    ).run()
    assert result.status == "success"  # warns, does not gate
    assert result.schema_drift is not None
    assert result.schema_drift[0]["arguments_valid"] is False


def test_clean_capsule_has_no_schema_drift(tmp_path: Path) -> None:
    cap = _capsule_with_tool_calls(tmp_path, [
        _record(),
        _record(arguments_schema_ref=None, result_schema_ref=None),
    ])
    base = tmp_path / "replays"
    result = ReplayEngine(
        capsule_dir=cap, flags=ReplayFlags(mode="forensic"), base_dir=base
    ).run()
    assert result.schema_drift is None
    data = yaml.safe_load((base / result.replay_id / "replay_result.yaml").read_text())
    assert "schema_drift" not in data


def test_revalidate_never_raises_on_malformed_record(tmp_path: Path) -> None:
    drift = revalidate_tool_calls([{"arguments_schema_ref": 42}], tmp_path)
    assert drift == []
