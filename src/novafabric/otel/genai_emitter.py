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

"""OTel GenAI canonical-span emitter (NF-032, ADR-0098).

Maps an **already-captured** capsule outward to OTel GenAI ``gen_ai.*`` spans — the
portable form of a run. The capsule's ``model-calls.jsonl`` already stores the OTel
GenAI semconv attributes (``gen_ai.request.model`` etc.), so this emitter wraps each
record in an OTLP-shaped span envelope and adds the two NovaFabric markers every span
carries:

- ``novafabric.mapping_version`` — the mapping-table version, so a consumer can tell
  which capsule↔OTLP mapping produced the span (R3);
- ``novafabric.semconv_maturity`` — ``development`` on agent/framework/tool spans (the
  OTel GenAI agent spans are Development-status in early 2026), ``stable`` on LLM client
  spans (R4). Honest labeling — no claim that agent spans are Stable.

Message/choice **content** is omitted by default; only when ``capture_content=True`` is
it routed through the ADR-0009 redaction gate by :mod:`novafabric.otel.content_bridge`.
Nothing is invented: a span exists only for a recorded model/tool call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from novafabric.otel.content_bridge import bridge_messages

#: version of the capsule ↔ OTel GenAI mapping table (R3).
MAPPING_VERSION = "1.0.0"

_MATURITY_STABLE = "stable"
_MATURITY_DEV = "development"


def _hex(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:n]


def _unix_nano(ts: Any) -> int:
    """ISO-8601 timestamp → Unix nanoseconds; 0 when absent/unparseable."""
    if not ts or not isinstance(ts, str):
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(dt.timestamp() * 1_000_000_000)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _span(
    *,
    name: str,
    kind: str,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    start: Any,
    end: Any,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    span: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "traceId": trace_id,
        "spanId": span_id,
        "startTimeUnixNano": _unix_nano(start),
        "endTimeUnixNano": _unix_nano(end),
        "attributes": {
            **attributes,
            "novafabric.mapping_version": MAPPING_VERSION,
        },
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return span


def emit_spans(
    capsule_dir: str | Path, *, capture_content: bool = False
) -> list[dict[str, Any]]:
    """Emit OTLP-shaped OTel GenAI spans for a stored capsule (NF-032).

    Returns a root ``invoke_agent`` span followed by one ``chat`` client span per
    recorded model call and one ``execute_tool`` span per recorded tool call. With
    ``capture_content`` false (default) no message/choice content is attached (R7 of the
    content-opt-in gate); when true, content is redacted+bounded via the content bridge.
    """
    capsule_dir = Path(capsule_dir)
    manifest: dict[str, Any] = {}
    manifest_path = capsule_dir / "capsule.yaml"
    if manifest_path.exists():
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - fail-open
            manifest = {}

    run_id = str(manifest.get("run_id", capsule_dir.name))
    trace_id = _hex(run_id, 32)
    agent_span_id = _hex(f"{run_id}:agent", 16)
    agent_name = str(manifest.get("agent_name") or manifest.get("model") or "agent")

    spans: list[dict[str, Any]] = [
        _span(
            name=f"invoke_agent {agent_name}",
            kind="SPAN_KIND_INTERNAL",
            trace_id=trace_id,
            span_id=agent_span_id,
            parent_span_id=None,
            start=manifest.get("created_at"),
            end=manifest.get("finished_at"),
            attributes={
                "gen_ai.system": str(manifest.get("provider", "novafabric")),
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.id": run_id,
                "gen_ai.agent.name": agent_name,
                "novafabric.semconv_maturity": _MATURITY_DEV,
            },
        )
    ]

    # LLM client spans (Stable semconv) from model-calls.jsonl.
    for i, rec in enumerate(_read_jsonl(capsule_dir / "model-calls.jsonl")):
        attrs: dict[str, Any] = {
            key: rec[key]
            for key in (
                "gen_ai.system",
                "gen_ai.operation.name",
                "gen_ai.request.model",
                "gen_ai.response.model",
                "gen_ai.usage.input_tokens",
                "gen_ai.usage.output_tokens",
                "gen_ai.response.finish_reasons",
            )
            if key in rec
        }
        attrs.setdefault("gen_ai.operation.name", "chat")
        attrs["novafabric.semconv_maturity"] = _MATURITY_STABLE
        messages = bridge_messages(rec.get("gen_ai.request.messages"), enabled=capture_content)
        if messages is not None:
            attrs["gen_ai.request.messages"] = messages
        spans.append(
            _span(
                name=f"chat {attrs.get('gen_ai.request.model', 'model')}",
                kind="SPAN_KIND_CLIENT",
                trace_id=trace_id,
                span_id=_hex(f"{run_id}:model:{i}", 16),
                parent_span_id=agent_span_id,
                start=rec.get("started_at"),
                end=rec.get("finished_at"),
                attributes=attrs,
            )
        )

    # Tool/framework spans (Development semconv) from tool-calls.jsonl.
    for i, rec in enumerate(_read_jsonl(capsule_dir / "tool-calls.jsonl")):
        tool_name = str(rec.get("tool_name", "tool"))
        spans.append(
            _span(
                name=f"execute_tool {tool_name}",
                kind="SPAN_KIND_INTERNAL",
                trace_id=trace_id,
                span_id=_hex(f"{run_id}:tool:{i}", 16),
                parent_span_id=agent_span_id,
                start=rec.get("started_at"),
                end=rec.get("finished_at"),
                attributes={
                    "gen_ai.system": str(manifest.get("provider", "novafabric")),
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool_name,
                    "novafabric.semconv_maturity": _MATURITY_DEV,
                },
            )
        )

    return spans
