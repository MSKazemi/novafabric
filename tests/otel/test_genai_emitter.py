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

"""Tests for NF-032/033 OTel GenAI span emitter + content bridge (ADR-0098)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from novafabric.otel.content_bridge import bridge_messages, redact_text
from novafabric.otel.genai_emitter import MAPPING_VERSION, emit_spans


def _capsule(
    tmp_path: Path,
    *,
    model_calls: list[dict] | None = None,
    tool_calls: list[dict] | None = None,
    manifest_extra: dict | None = None,
) -> Path:
    d = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    d.mkdir()
    manifest = {
        "run_id": "run-otel",
        "provider": "openai",
        "created_at": "2026-07-04T00:00:00Z",
        "finished_at": "2026-07-04T00:00:02Z",
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    (d / "capsule.yaml").write_text(yaml.dump(manifest))
    if model_calls is not None:
        (d / "model-calls.jsonl").write_text(
            "\n".join(json.dumps(r) for r in model_calls) + "\n"
        )
    if tool_calls is not None:
        (d / "tool-calls.jsonl").write_text(
            "\n".join(json.dumps(r) for r in tool_calls) + "\n"
        )
    return d


# ── root agent span + maturity/version markers (R3/R4) ───────────────────────


def test_emit_root_agent_span(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    spans = emit_spans(cap)
    assert len(spans) == 1
    root = spans[0]
    assert root["attributes"]["gen_ai.operation.name"] == "invoke_agent"
    assert root["attributes"]["novafabric.semconv_maturity"] == "development"
    assert root["attributes"]["novafabric.mapping_version"] == MAPPING_VERSION
    assert "parentSpanId" not in root


def test_client_span_is_stable_and_parented(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        model_calls=[
            {
                "gen_ai.system": "openai",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.response.model": "gpt-4o",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 5,
                "started_at": "2026-07-04T00:00:00Z",
                "finished_at": "2026-07-04T00:00:01Z",
            }
        ],
    )
    spans = emit_spans(cap)
    client = next(s for s in spans if s["kind"] == "SPAN_KIND_CLIENT")
    assert client["attributes"]["novafabric.semconv_maturity"] == "stable"
    assert client["attributes"]["gen_ai.request.model"] == "gpt-4o"
    assert client["attributes"]["gen_ai.operation.name"] == "chat"
    assert client["parentSpanId"] == spans[0]["spanId"]
    assert client["startTimeUnixNano"] > 0


def test_tool_span_is_development(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        tool_calls=[{"tool_name": "web_search", "started_at": "2026-07-04T00:00:00Z"}],
    )
    spans = emit_spans(cap)
    tool = next(s for s in spans if s["attributes"]["gen_ai.operation.name"] == "execute_tool")
    assert tool["attributes"]["gen_ai.tool.name"] == "web_search"
    assert tool["attributes"]["novafabric.semconv_maturity"] == "development"


def test_all_spans_share_trace_id(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        model_calls=[{"gen_ai.request.model": "m", "started_at": "2026-07-04T00:00:00Z"}],
        tool_calls=[{"tool_name": "t"}],
    )
    spans = emit_spans(cap)
    trace_ids = {s["traceId"] for s in spans}
    assert len(trace_ids) == 1
    assert len(next(iter(trace_ids))) == 32


def test_deterministic_ids(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, model_calls=[{"gen_ai.request.model": "m"}])
    assert emit_spans(cap) == emit_spans(cap)


# ── content opt-in gate (NF-033) ─────────────────────────────────────────────


def test_content_omitted_by_default(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        model_calls=[
            {
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.request.messages": [{"role": "user", "content": "hello"}],
            }
        ],
    )
    spans = emit_spans(cap)
    client = next(s for s in spans if s["kind"] == "SPAN_KIND_CLIENT")
    assert "gen_ai.request.messages" not in client["attributes"]


def test_content_included_and_redacted_when_opted_in(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        model_calls=[
            {
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.request.messages": [
                    {"role": "user", "content": "my key is sk-ant-aaaaaaaaaaaaaaaaaaaaaa"}
                ],
            }
        ],
    )
    spans = emit_spans(cap, capture_content=True)
    client = next(s for s in spans if s["kind"] == "SPAN_KIND_CLIENT")
    msgs = client["attributes"]["gen_ai.request.messages"]
    assert "sk-ant-" not in json.dumps(msgs)
    assert "[REDACTED:anthropic-api-key]" in json.dumps(msgs)


# ── content bridge unit ──────────────────────────────────────────────────────


def test_bridge_off_returns_none() -> None:
    assert bridge_messages([{"role": "user", "content": "hi"}], enabled=False) is None
    assert bridge_messages(None, enabled=True) is None


def test_redact_text_masks_secret() -> None:
    out = redact_text("token hf_" + "a" * 40)
    assert "hf_" + "a" * 40 not in out
    assert "[REDACTED:huggingface-token]" in out


def test_bridge_bounds_length(tmp_path: Path) -> None:
    long = {"role": "user", "content": "x" * 10000}
    out = bridge_messages([long], enabled=True)
    assert out is not None
    assert len(out[0]["content"]) <= 4000


# ── robustness ───────────────────────────────────────────────────────────────


def test_missing_files_yield_only_agent_span(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    assert len(emit_spans(cap)) == 1


def test_malformed_jsonl_line_skipped(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "model-calls.jsonl").write_text('{"gen_ai.request.model": "m"}\nnot json\n')
    spans = emit_spans(cap)
    assert len([s for s in spans if s["kind"] == "SPAN_KIND_CLIENT"]) == 1


def test_no_capsule_yaml_uses_dir_name(tmp_path: Path) -> None:
    d = tmp_path / "bare-run"
    d.mkdir()
    spans = emit_spans(d)
    assert spans[0]["attributes"]["gen_ai.agent.id"] == "bare-run"


def test_bad_timestamp_is_zero(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, manifest_extra={"created_at": "not-a-date"})
    assert emit_spans(cap)[0]["startTimeUnixNano"] == 0


def test_unparseable_capsule_yaml_fails_open(tmp_path: Path) -> None:
    d = tmp_path / "broken"
    d.mkdir()
    (d / "capsule.yaml").write_text("::: not: valid: yaml: [")
    spans = emit_spans(d)
    assert spans[0]["attributes"]["gen_ai.agent.id"] == "broken"


def test_bridge_redacts_nested_list_and_dict_content() -> None:
    nested = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "leak hf_" + "b" * 40},
                "sk-ant-cccccccccccccccccccccc",
            ],
        }
    ]
    out = bridge_messages(nested, enabled=True)
    dumped = json.dumps(out)
    assert "hf_" not in dumped
    assert "sk-ant-" not in dumped
    assert "[REDACTED:huggingface-token]" in dumped


def test_cli_emit_otel_genai(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    runner = CliRunner()
    out_base = tmp_path / "runs"
    result = runner.invoke(
        app,
        ["capture", "--no-daemon", "--emit-otel-genai", "-o", str(out_base),
         "python", "-c", "print('hi')"],
    )
    assert result.exit_code == 0, result.output
    assert "OTel GenAI spans" in result.output
    span_files = list(out_base.glob("*/otel-genai-spans.json"))
    assert span_files, "expected an otel-genai-spans.json under the capsule"
    spans = json.loads(span_files[0].read_text())
    assert spans[0]["attributes"]["gen_ai.operation.name"] == "invoke_agent"
