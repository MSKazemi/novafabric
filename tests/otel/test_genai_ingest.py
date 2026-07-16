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

"""Tests for NF-034 OTLP/HTTP JSON GenAI ingest (ADR-0098)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from novafabric.otel.genai_emitter import MAPPING_VERSION, emit_spans
from novafabric.otel.genai_ingest import (
    CAPTURE_LEVEL,
    GenAIIngestResult,
    OTLPIngestError,
    ingest_otlp_json,
    parse_otlp_json,
    write_ingest_capsule,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "otlp"


def _valid_payload() -> dict:
    return json.loads((FIXTURES / "genai-traces-valid.json").read_text())


def _invalid_payload() -> dict:
    return json.loads((FIXTURES / "genai-traces-invalid.json").read_text())


# ── parse_otlp_json ───────────────────────────────────────────────────────────


def test_parse_flattens_all_spans() -> None:
    spans = parse_otlp_json(_valid_payload())
    assert len(spans) == 4
    names = [s["name"] for s in spans]
    assert "chat gpt-4o" in names and "execute_tool search_web" in names


def test_parse_decodes_otlp_any_values() -> None:
    spans = parse_otlp_json(_valid_payload())
    chat = next(s for s in spans if s["name"] == "chat gpt-4o")
    attrs = chat["attributes"]
    assert attrs["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.usage.input_tokens"] == 42  # intValue string → int
    assert attrs["gen_ai.request.temperature"] == 0.2
    assert attrs["gen_ai.response.finish_reasons"] == ["stop"]
    assert chat["parent_span_id"] == "aaaa19b7ec3c1b17"
    assert chat["start_unix_nano"] == 1783420800500000000


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"nope": []},
        {"resourceSpans": "not-a-list"},
        {"resourceSpans": ["not-a-dict"]},
        {"resourceSpans": [{"scopeSpans": "nope"}]},
        {"resourceSpans": [{"scopeSpans": ["nope"]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": "nope"}]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": ["nope"]}]}]},
    ],
)
def test_parse_rejects_malformed_payloads(payload: object) -> None:
    with pytest.raises(OTLPIngestError):
        parse_otlp_json(payload)


def test_parse_rejects_golden_invalid_fixture() -> None:
    with pytest.raises(OTLPIngestError):
        parse_otlp_json(_invalid_payload())


def test_parse_accepts_plain_dict_attributes_and_kvlist() -> None:
    payload = {
        "resourceSpans": [{"scopeSpans": [{"spans": [
            {
                "name": "chat m",
                "spanId": "ab" * 8,
                "attributes": {"gen_ai.request.model": "m"},
            },
            {
                "name": "chat n",
                "spanId": "cd" * 8,
                "attributes": [
                    {"key": "gen_ai.request.model", "value": {"stringValue": "n"}},
                    {"key": "meta", "value": {"kvlistValue": {"values": [
                        {"key": "k", "value": {"boolValue": True}},
                    ]}}},
                    {"key": "blob", "value": {"bytesValue": "aGk="}},
                    {"key": "bad-int", "value": {"intValue": "not-an-int"}},
                ],
            },
        ]}]}]
    }
    spans = parse_otlp_json(payload)
    assert spans[0]["attributes"] == {"gen_ai.request.model": "m"}
    attrs = spans[1]["attributes"]
    assert attrs["meta"] == {"k": True}
    assert attrs["blob"] == "aGk="
    assert attrs["bad-int"] == "not-an-int"  # degrades to raw, never crashes


# ── ingest_otlp_json mapping ─────────────────────────────────────────────────


def test_ingest_maps_chat_and_tool_spans() -> None:
    result = ingest_otlp_json(_valid_payload())
    assert len(result.model_calls) == 1
    assert len(result.tool_calls) == 1
    assert result.agent_span_count == 1
    assert result.spans_seen == 4
    assert result.skipped_spans == 1  # the http.* span — never guessed at
    assert result.genai_spans == 3

    mc = result.model_calls[0]
    assert mc["gen_ai.request.model"] == "gpt-4o"
    assert mc["gen_ai.usage.output_tokens"] == 7
    assert mc["status"] == "success"
    assert mc["started_at"].startswith("2026-07-07T")
    assert mc["duration_ms"] == 1000
    assert mc["novafabric.mapping_version"] == MAPPING_VERSION

    tc = result.tool_calls[0]
    assert tc["tool_name"] == "search_web"
    assert tc["gen_ai.tool.call.id"] == "call_0001"
    assert tc["novafabric.mapping_version"] == MAPPING_VERSION

    assert result.agent == {"agent_name": "summarizer", "provider": "openai"}


def test_ingest_never_fabricates_content() -> None:
    """No message attrs in the span ⇒ no message keys in the event (ADR-0021)."""
    result = ingest_otlp_json(_valid_payload())
    mc = result.model_calls[0]
    for key in ("gen_ai.request.messages", "gen_ai.input.messages",
                "gen_ai.output.messages", "gen_ai.response.choices"):
        assert key not in mc


def test_ingest_carries_content_only_when_present() -> None:
    messages = [{"role": "user", "content": "hello"}]
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "chat m",
        "attributes": {
            "gen_ai.request.model": "m",
            "gen_ai.input.messages": messages,
        },
    }]}]}]}
    result = ingest_otlp_json(payload)
    assert result.model_calls[0]["gen_ai.input.messages"] == messages


def test_ingest_unknown_genai_attrs_carried_and_enumerated() -> None:
    result = ingest_otlp_json(_valid_payload())
    mc = result.model_calls[0]
    assert mc["otlp.unmapped"] == {"gen_ai.future.attribute": "unknown-to-mapping-v1"}
    assert result.unmapped_keys == ["gen_ai.future.attribute"]
    # Other-namespace attrs are dropped but enumerated, never silent.
    assert "http.request.method" not in result.dropped_keys  # skipped span: not ingested
    assert result.dropped_keys == []


def test_ingest_error_status_span() -> None:
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "execute_tool boom",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": {"gen_ai.tool.name": "boom"},
        "status": {"code": "STATUS_CODE_ERROR", "message": "tool timeout"},
    }]}]}]}
    result = ingest_otlp_json(payload)
    tc = result.tool_calls[0]
    assert tc["status"] == "error"
    assert tc["error"]["type"] == "OTLPStatusError"
    assert tc["error"]["message"] == "tool timeout"


def test_ingest_unclassified_genai_span_skipped_honestly() -> None:
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "mystery",
        "attributes": {"gen_ai.something.odd": "x"},
    }]}]}]}
    result = ingest_otlp_json(payload)
    assert result.unclassified_spans == 1
    assert result.genai_spans == 0
    assert result.model_calls == [] and result.tool_calls == []


def test_ingest_no_timestamps_omits_time_fields() -> None:
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "chat m",
        "attributes": {"gen_ai.request.model": "m"},
    }]}]}]}
    result = ingest_otlp_json(payload)
    mc = result.model_calls[0]
    assert "started_at" not in mc and "finished_at" not in mc
    assert "duration_ms" not in mc


def test_ingest_tool_name_falls_back_to_span_name() -> None:
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "execute_tool anon",
        "attributes": {"gen_ai.operation.name": "execute_tool"},
    }]}]}]}
    result = ingest_otlp_json(payload)
    assert result.tool_calls[0]["tool_name"] == "execute_tool anon"


def test_ingest_dropped_other_namespace_keys_enumerated() -> None:
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "chat m",
        "attributes": {"gen_ai.request.model": "m", "server.address": "example.com"},
    }]}]}]}
    result = ingest_otlp_json(payload)
    assert result.dropped_keys == ["server.address"]
    assert "server.address" not in result.model_calls[0]


# ── round-trip with the emitter (inverse mapping) ─────────────────────────────


def test_round_trip_emitter_to_ingest(tmp_path: Path) -> None:
    """emit_spans output re-ingests: gen_ai.* keys survive the round trip."""
    cap = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.dump({
        "run_id": "run-rt",
        "provider": "openai",
        "created_at": "2026-07-04T00:00:00Z",
        "finished_at": "2026-07-04T00:00:02Z",
    }))
    model_call = {
        "gen_ai.system": "openai",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.response.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 5,
        "gen_ai.response.finish_reasons": ["stop"],
        "started_at": "2026-07-04T00:00:00.500000Z",
        "finished_at": "2026-07-04T00:00:01.500000Z",
    }
    (cap / "model-calls.jsonl").write_text(json.dumps(model_call) + "\n")
    (cap / "tool-calls.jsonl").write_text(json.dumps({
        "tool_name": "search", "started_at": "2026-07-04T00:00:01.600000Z",
        "finished_at": "2026-07-04T00:00:01.900000Z",
    }) + "\n")

    spans = emit_spans(cap)
    result = ingest_otlp_json({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]})

    assert result.agent_span_count == 1
    assert len(result.model_calls) == 1 and len(result.tool_calls) == 1
    mc = result.model_calls[0]
    for key in (k for k in model_call if k.startswith("gen_ai.")):
        assert mc[key] == model_call[key]
    assert mc["started_at"] == model_call["started_at"]
    assert result.tool_calls[0]["tool_name"] == "search"
    # The emitter's own markers are recognized, not flagged as unknown.
    assert result.unmapped_keys == []


# ── write_ingest_capsule ──────────────────────────────────────────────────────


def test_write_capsule_passes_nova_validate(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app as cli_app

    result = ingest_otlp_json(_valid_payload())
    cdir = write_ingest_capsule(result, tmp_path)

    for fname in ("capsule.yaml", "env.lock", "redaction-proof.json", "replay.yaml",
                  "trace.jsonl", "model-calls.jsonl", "tool-calls.jsonl", "assets.jsonl"):
        assert (cdir / fname).exists(), fname

    run = CliRunner().invoke(cli_app, ["validate", str(cdir)])
    assert run.exit_code == 0, run.output
    assert "Valid capsule" in run.output


def test_write_capsule_manifest_is_honest(tmp_path: Path) -> None:
    result = ingest_otlp_json(_valid_payload())
    cdir = write_ingest_capsule(result, tmp_path)
    manifest = yaml.safe_load((cdir / "capsule.yaml").read_text())

    assert manifest["capture_mode"] == "otel-import"
    assert manifest["metadata"]["capture_level"] == CAPTURE_LEVEL
    assert manifest["metadata"]["otlp.mapping_version"] == MAPPING_VERSION
    assert manifest["metadata"]["otlp.agent_name"] == "summarizer"
    assert manifest["metadata"]["otlp.spans_skipped"] == "1"
    assert manifest["model_call_count"] == 1
    assert manifest["tool_call_count"] == 1
    assert manifest["status"] == "success"
    assert manifest["command"] == []
    # Times derived from the ingested spans, not invented.
    assert manifest["created_at"] == "2026-07-07T10:40:00.500000Z"
    assert manifest["duration_ms"] == 1600

    model_lines = (cdir / "model-calls.jsonl").read_text().splitlines()
    assert len(model_lines) == 1
    rec = json.loads(model_lines[0])
    assert rec["gen_ai.request.model"] == "gpt-4o"
    assert "model_call_id" in rec


def test_write_capsule_error_events_mark_partial(tmp_path: Path) -> None:
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "execute_tool boom",
        "attributes": {"gen_ai.tool.name": "boom"},
        "status": {"code": 2},
    }]}]}]}
    cdir = write_ingest_capsule(ingest_otlp_json(payload), tmp_path)
    manifest = yaml.safe_load((cdir / "capsule.yaml").read_text())
    assert manifest["status"] == "partial"
    # No span timestamps ⇒ created_at falls back to ingest time, never crashes.
    assert manifest["created_at"]


def test_write_capsule_redacts_ingested_secrets(tmp_path: Path) -> None:
    """ADR-0009: foreign span content passes the secret scanner before sealing."""
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "chat m",
        "attributes": {
            "gen_ai.request.model": "m",
            "gen_ai.input.messages": [
                {"role": "user", "content": "use key sk-ABCDEFGHIJKLMNOPQRSTUVWX01"}
            ],
        },
    }]}]}]}
    cdir = write_ingest_capsule(ingest_otlp_json(payload), tmp_path)
    content = (cdir / "model-calls.jsonl").read_text()
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX01" not in content
    assert "REDACTED" in content
    proof = json.loads((cdir / "redaction-proof.json").read_text())
    assert proof["capsule_run_id"] == cdir.name


def test_write_capsule_respects_explicit_run_id(tmp_path: Path) -> None:
    cdir = write_ingest_capsule(
        ingest_otlp_json(_valid_payload()), tmp_path,
        run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX",
    )
    assert cdir.name == "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def test_empty_result_capsule_falls_back_to_now(tmp_path: Path) -> None:
    cdir = write_ingest_capsule(GenAIIngestResult(), tmp_path)
    manifest = yaml.safe_load((cdir / "capsule.yaml").read_text())
    assert manifest["model_call_count"] == 0
    assert manifest["created_at"] == manifest["finished_at"]
    assert manifest["duration_ms"] == 0
