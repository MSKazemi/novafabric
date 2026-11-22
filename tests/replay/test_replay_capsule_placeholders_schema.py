"""The replay capsule's placeholder records validate against the shipped
``tool-call`` / ``model-call`` schemas and carry only what replay observed.

A mocked replay writes one placeholder per call it did not serve
(``_replay_capsule.replayed_streams``). They used to be ``{tool_call_id:
"replay-live-2", tool_name, status: "live", extensions}``: no ULID, a status
outside the enum and 15 required fields missing, so no schema-checking reader
could load the replay capsule. The schemas now admit a record marked
``extensions["io.novafabric.replay_placeholder"]`` with only the fields replay
knows (additive: every record without the mark is validated exactly as before).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.replay._replay_capsule import PLACEHOLDER_EXT, replayed_streams
from novafabric.replay._tool_call_outcomes import AFTER_DIVERGENCE

REPO = Path(__file__).resolve().parents[2]
#: The in-force copies (same ``$id``); ``schemas/`` is the OAS v1.0 target, whose
#: records are ``schema_version: 1.x`` -- a replay capsule (0.1.0) never targets it.
_COPIES = (REPO / "src" / "novafabric" / "schemas", REPO / "ui" / "dashboard" / "src" / "data"
           / "schemas")
LC = "langchain_core.tools.BaseTool"
MCP = "mcp.ClientSession.call_tool"
_UNOBSERVED_TOOL = ("parent_span_id", "started_at", "finished_at", "duration_ms",
                    "arguments", "result", "tool_version", "tool_provider", "agent_call_id")
_UNOBSERVED_MODEL = ("parent_span_id", "started_at", "finished_at", "duration_ms",
                     "gen_ai.request.messages", "gen_ai.response.choices",
                     "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")


def _schemas(name: str) -> list[dict[str, Any]]:
    return [json.loads((d / f"{name}.schema.json").read_text()) for d in _COPIES]


def _validate(record: dict[str, Any], name: str) -> None:
    for schema in _schemas(name):
        jsonschema.validate(record, schema, format_checker=jsonschema.FormatChecker())


def _events() -> list[dict[str, Any]]:
    return [
        {"event": "model_live", "pid": 1, "provider": "openai", "surface": "openai.chat"},
        {"event": "model_live", "pid": 1, "provider": None, "surface": None},
        {"event": "divergence", "pid": 1, "kind": "tool_result_not_servable",
         "tool_name": "ls", "surface": LC, "mutation_class": "unknown"},
        {"event": "tool_live", "pid": 1, "tool_name": "ls", "surface": LC,
         "mutation_class": "unknown", AFTER_DIVERGENCE: True},
        {"event": "tool_refused", "pid": 1, "tool_name": "rm", "surface": LC,
         "mutation_class": "unknown"},
        # strict: the divergence raised, nothing followed
        {"event": "divergence", "pid": 1, "kind": "tool_call_unmatched",
         "tool_name": "fetch", "surface": MCP, "mutation_class": "unknown"},
        {"event": "tool_live", "pid": 1, "tool_name": "fetch", "surface": None,
         "mutation_class": "unknown", "override": "allow", "consumed": False},
    ]


def test_every_placeholder_validates_and_holds_nothing_unobserved() -> None:
    models, tools = replayed_streams([], [], _events())
    assert [m["extensions"][PLACEHOLDER_EXT] for m in models] == ["live", "live"]
    assert [t["extensions"][PLACEHOLDER_EXT] for t in tools] == [
        "live", "refused", "unmatched", "live",
    ]
    for record in models:
        _validate(record, "model-call")
        for key in _UNOBSERVED_MODEL:
            assert key not in record, (key, record)
        assert "status" not in record  # it ran live: replay never saw how it ended
    for record in tools:
        _validate(record, "tool-call")
        for key in _UNOBSERVED_TOOL:
            assert key not in record, (key, record)
    assert models[0]["gen_ai.system"] == "openai"
    assert "gen_ai.system" not in models[1]  # unknown provider: absent, not invented
    live, refused, unmatched, allowed = tools
    assert "status" not in live and "status" not in allowed
    assert refused["status"] == unmatched["status"] == "denied"  # refused before the body
    assert [t["tool_name"] for t in tools] == ["ls", "rm", "fetch", "fetch"]
    assert live["mutation_class"] == "unknown"
    assert allowed["extensions"]["io.novafabric.replay_surface"] == MCP
    ids = [r["model_call_id"] for r in models] + [r["tool_call_id"] for r in tools]
    assert len(set(ids)) == len(ids)  # fresh ULIDs, unique within the replay capsule


@pytest.mark.parametrize("name", ["tool-call", "model-call"])
def test_a_record_without_the_mark_still_needs_every_required_field(name: str) -> None:
    """The relaxation is scoped to placeholders: a bare record is still invalid."""
    models, tools = replayed_streams([], [], _events())
    record = dict(models[0] if name == "model-call" else tools[0])
    record["extensions"] = {
        k: v for k, v in record["extensions"].items() if k != PLACEHOLDER_EXT
    }
    for schema in _schemas(name):
        with pytest.raises(jsonschema.ValidationError, match="required property"):
            jsonschema.validate(record, schema)


@pytest.mark.parametrize("name", ["tool-call", "model-call"])
def test_a_placeholder_mark_outside_the_enum_is_rejected(name: str) -> None:
    models, tools = replayed_streams([], [], _events())
    record = json.loads(json.dumps(models[0] if name == "model-call" else tools[0]))
    record["extensions"][PLACEHOLDER_EXT] = "served"
    for schema in _schemas(name):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(record, schema)
