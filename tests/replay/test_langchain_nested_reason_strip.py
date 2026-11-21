"""ADR-0306 r3b: the nesting text an earlier LangChain capture wrote is policy,
not data. ``_contract.not_servable_reason`` strips exactly that text (for the
record's own ``nested_records`` count) and keeps every other reason; the
LangChain server upgrades a record whose only obstacle it was to ``json-v1``.
The ADK and Agents SDK seams still write it, and it still blocks them.

Unit tests over record dicts; no capture, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from novafabric.capture import _tool_codec as codec
from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON
from novafabric.replay._contract import not_servable_reason, upgrade_legacy_langchain_record

NESTED = codec.seam_nested_reason("LangChain tool", 3)


def _record(reason: str, *, surface: str = codec.TOOL_SURFACE_LANGCHAIN_TOOL,
            nested: Any = 3, value: bool = True, status: str = "success") -> dict[str, Any]:
    ext: dict[str, Any] = {
        codec.TOOL_SURFACE_EXT: surface,
        codec.RESULT_CODEC_EXT: codec.CODEC_NOT_SERVABLE,
        codec.NOT_SERVABLE_REASON_EXT: reason,
    }
    if nested is not None:
        ext[codec.NESTED_RECORDS_EXT] = nested
    return {
        "tool_call_id": "t1", "tool_name": "task", "transport": "python",
        "mutation_class": "unknown", "arguments": {}, "status": status,
        "result": {"value": "done"} if value else None, "extensions": ext,
    }


def test_the_nesting_text_alone_is_stripped_and_the_kept_value_served() -> None:
    record = _record(NESTED)
    assert not_servable_reason(record) is None
    upgraded = upgrade_legacy_langchain_record(record)
    assert upgraded is not record
    assert upgraded["extensions"][codec.RESULT_CODEC_EXT] == codec.CODEC_JSON
    assert codec.NOT_SERVABLE_REASON_EXT not in upgraded["extensions"]
    assert codec.decode_result(upgraded) == (True, "done")
    assert record["extensions"][codec.RESULT_CODEC_EXT] == codec.CODEC_NOT_SERVABLE


def test_without_a_kept_value_the_record_holds_nothing_to_serve() -> None:
    assert not_servable_reason(_record(NESTED, value=False)) == (
        "the record holds no result value"
    )


@pytest.mark.parametrize(("reason", "left"), [
    (f"{PAYLOADS_OFF_REASON}; {NESTED}", PAYLOADS_OFF_REASON),
    (f"{NESTED}; the result is a set", "the result is a set"),
    (f"a; {NESTED}; b", "a; b"),
])
def test_every_other_reason_is_kept(reason: str, left: str) -> None:
    record = _record(reason, value=False)
    assert not_servable_reason(record) == left
    assert upgrade_legacy_langchain_record(record) is record


@pytest.mark.parametrize("nested", [2, None, 0, "3"])
def test_the_text_is_stripped_only_for_the_records_own_count(nested: Any) -> None:
    assert not_servable_reason(_record(NESTED, nested=nested)) == NESTED


@pytest.mark.parametrize("surface", [codec.TOOL_SURFACE_ADK_TOOL,
                                     codec.TOOL_SURFACE_OPENAI_AGENTS_TOOL])
def test_other_framework_seams_stay_unservable_when_nested(surface: str) -> None:
    for subject in ("ADK tool", "Agents SDK tool"):
        reason = codec.seam_nested_reason(subject, 3)
        assert not_servable_reason(_record(reason, surface=surface)) == reason


def test_a_failure_is_never_upgraded() -> None:
    record = _record(NESTED, status="error", value=False)
    assert upgrade_legacy_langchain_record(record) is record
