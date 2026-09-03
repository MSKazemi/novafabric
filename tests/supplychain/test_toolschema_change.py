"""NF-164 tool-schema change classification (ADR-0148 D2).

The additive-safe rule is easy to state and easy to implement wrongly in three specific places,
so each has its own test: a **new required** property is an addition that breaks every recorded
payload; `unknown` must be reachable and must never read as safe; and truncating the bounded diff
must never soften the class.
"""

from __future__ import annotations

import pytest

from novafabric.supplychain.toolschema.change import (
    FACET_NAME,
    MAX_DIFF_ENTRIES,
    UNTRAVERSED_KEYWORDS,
    ToolSchemaChange,
    ToolSchemaChangeError,
    attach_facet,
    classify_schema_change,
    facet_from_capsule,
    schema_digest,
)

BASE = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "top_k": {"type": "integer"},
    },
    "required": ["query"],
}


def _classify(to_schema: dict, from_schema: dict | None = None, **kwargs) -> ToolSchemaChange:
    return classify_schema_change(
        tool_id="mcp://acme/search",
        from_schema=from_schema if from_schema is not None else BASE,
        to_schema=to_schema,
        **kwargs,
    )


# ── The additive-safe rule ───────────────────────────────────────────────


def test_an_identical_schema_is_additive_with_no_diff() -> None:
    change = _classify(BASE)
    assert change.change_class == "additive"
    assert change.diff == []
    assert change.from_schema_digest == change.to_schema_digest


def test_a_new_optional_property_is_additive() -> None:
    to_schema = {**BASE, "properties": {**BASE["properties"], "lang": {"type": "string"}}}
    change = _classify(to_schema)
    assert change.change_class == "additive"
    assert [(d.op, d.path) for d in change.diff] == [("added", "$.properties.lang")]


def test_a_new_required_property_is_breaking_not_additive() -> None:
    """An *addition* that breaks every payload ever recorded — the case most easily got wrong."""
    to_schema = {
        "type": "object",
        "properties": {**BASE["properties"], "tenant": {"type": "string"}},
        "required": ["query", "tenant"],
    }
    change = _classify(to_schema)
    assert change.change_class == "breaking"
    assert [(d.op, d.path, d.now) for d in change.diff] == [
        ("tightened", "$.properties.tenant", "required")
    ]


def test_removing_a_property_is_breaking() -> None:
    to_schema = {"type": "object", "properties": {"query": {"type": "string"}},
                 "required": ["query"]}
    change = _classify(to_schema)
    assert change.change_class == "breaking"
    assert [(d.op, d.path) for d in change.diff] == [("removed", "$.properties.top_k")]


def test_tightening_optional_to_required_is_breaking() -> None:
    to_schema = {**BASE, "required": ["query", "top_k"]}
    change = _classify(to_schema)
    assert change.change_class == "breaking"
    assert [(d.op, d.was, d.now) for d in change.diff] == [
        ("tightened", "optional", "required")
    ]


def test_a_type_change_is_breaking_and_names_both_types() -> None:
    to_schema = {**BASE, "properties": {**BASE["properties"], "top_k": {"type": "string"}}}
    change = _classify(to_schema)
    assert change.change_class == "breaking"
    entry = change.diff[0]
    assert (entry.op, entry.was, entry.now) == ("tightened", "integer", "string")


def test_a_union_type_is_rendered_and_compared() -> None:
    """JSON Schema allows `"type": ["string", "null"]`; narrowing one is a tightening."""
    union = {
        "type": "object",
        "properties": {"query": {"type": ["string", "null"]}},
        "required": ["query"],
    }
    narrowed = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    change = classify_schema_change(tool_id="t", from_schema=union, to_schema=narrowed)
    assert change.change_class == "breaking"
    entry = change.diff[0]
    assert (entry.op, entry.was, entry.now) == ("tightened", "null|string", "string")


def test_an_unchanged_union_type_is_not_a_change() -> None:
    union = {"type": "object", "properties": {"q": {"type": ["string", "null"]}}}
    assert classify_schema_change(
        tool_id="t", from_schema=union, to_schema=dict(union)
    ).diff == []


def test_nested_object_properties_are_compared() -> None:
    nested_from = {
        "type": "object",
        "properties": {"filter": {"type": "object", "properties": {"tag": {"type": "string"}}}},
    }
    nested_to = {
        "type": "object",
        "properties": {"filter": {"type": "object", "properties": {}}},
    }
    change = classify_schema_change(
        tool_id="t", from_schema=nested_from, to_schema=nested_to
    )
    assert change.change_class == "breaking"
    assert change.diff[0].path == "$.properties.filter.properties.tag"


# ── Deprecation, and precedence ──────────────────────────────────────────


def test_marking_a_property_deprecated_is_a_deprecation() -> None:
    to_schema = {
        **BASE,
        "properties": {**BASE["properties"], "top_k": {"type": "integer", "deprecated": True}},
    }
    change = _classify(to_schema)
    assert change.change_class == "deprecation"
    assert [(d.op, d.path) for d in change.diff] == [("deprecated", "$.properties.top_k")]


def test_breaking_beats_deprecation() -> None:
    """A release that deprecates one field and removes another is breaking."""
    to_schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "deprecated": True}},
        "required": ["query"],
    }
    change = _classify(to_schema)
    assert change.change_class == "breaking"
    assert {d.op for d in change.diff} == {"deprecated", "removed"}


def test_an_already_deprecated_property_is_not_a_new_deprecation() -> None:
    already = {
        "type": "object",
        "properties": {"query": {"type": "string", "deprecated": True}},
        "required": ["query"],
    }
    change = classify_schema_change(tool_id="t", from_schema=already, to_schema=already)
    assert change.change_class == "additive"
    assert change.diff == []


# ── unknown never defaults to safe ───────────────────────────────────────


@pytest.mark.parametrize("keyword", UNTRAVERSED_KEYWORDS)
def test_an_untraversed_keyword_is_unknown_with_a_reason(keyword: str) -> None:
    """Calling an unanalysed schema 'additive' would read as safe to ship."""
    to_schema = {**BASE, keyword: [{"type": "object"}] if keyword != "$ref" else "#/defs/x"}
    change = _classify(to_schema)
    assert change.change_class == "unknown"
    assert change.reason is not None and keyword in change.reason


def test_an_untraversed_keyword_nested_deep_is_still_unknown() -> None:
    to_schema = {
        **BASE,
        "properties": {
            **BASE["properties"],
            "query": {"type": "string", "anyOf": [{"minLength": 1}]},
        },
    }
    assert _classify(to_schema).change_class == "unknown"


def test_unknown_still_reports_the_differences_it_did_find() -> None:
    """'We could not classify' is not 'we found nothing'."""
    to_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "oneOf": [{"type": "object"}],
    }
    change = _classify(to_schema)
    assert change.change_class == "unknown"
    assert [d.op for d in change.diff] == ["removed"]


def test_a_non_object_schema_is_refused() -> None:
    with pytest.raises(ToolSchemaChangeError, match="not a JSON Schema object"):
        classify_schema_change(tool_id="t", from_schema=BASE, to_schema=["nope"])


# ── The bound must not soften the verdict ────────────────────────────────


def test_truncating_the_diff_does_not_change_the_class() -> None:
    """A breaking change past the cap must still classify as breaking."""
    props = {f"opt_{i}": {"type": "string"} for i in range(10)}
    to_schema = {
        "type": "object",
        # every optional addition sorts before "query", so the removal lands last
        "properties": props,
        "required": [],
    }
    change = classify_schema_change(
        tool_id="t", from_schema=BASE, to_schema=to_schema, max_diff_entries=2
    )
    assert change.change_class == "breaking", "the removals are beyond the cap"
    assert change.diff_truncated is True
    assert len(change.diff) == 2
    assert change.diff_total == 12  # 10 added + query removed + top_k removed


def test_an_untruncated_diff_says_so() -> None:
    change = _classify(BASE)
    assert change.diff_truncated is False
    assert change.diff_total == 0


def test_the_default_bound_is_applied() -> None:
    props = {f"opt_{i}": {"type": "string"} for i in range(MAX_DIFF_ENTRIES + 5)}
    change = classify_schema_change(
        tool_id="t",
        from_schema={"type": "object", "properties": {}},
        to_schema={"type": "object", "properties": props},
    )
    assert len(change.diff) == MAX_DIFF_ENTRIES
    assert change.diff_total == MAX_DIFF_ENTRIES + 5


def test_a_zero_bound_is_refused() -> None:
    with pytest.raises(ToolSchemaChangeError, match="at least 1"):
        _classify(BASE, max_diff_entries=0)


# ── Digests are over content, not file bytes ─────────────────────────────


def test_reformatting_is_not_a_change() -> None:
    """NF-165 hashes file bytes — right for 'which file?', wrong for 'which schema?'.

    Under a byte digest this pair would report two different digests with an empty diff: a
    recorded change containing no changes.
    """
    reordered = {
        "required": ["query"],
        "properties": {"top_k": {"type": "integer"}, "query": {"type": "string"}},
        "type": "object",
    }
    change = _classify(reordered)
    assert change.from_schema_digest == change.to_schema_digest
    assert change.diff == []
    assert change.change_class == "additive"


def test_the_digest_moves_when_the_content_does() -> None:
    """The reformat test above is only meaningful if the digest can move at all."""
    to_schema = {**BASE, "properties": {**BASE["properties"], "lang": {"type": "string"}}}
    change = _classify(to_schema)
    assert change.from_schema_digest != change.to_schema_digest


def test_schema_digest_is_deterministic() -> None:
    assert schema_digest(BASE) == schema_digest(dict(BASE))


# ── It classifies; it does not gate ──────────────────────────────────────


def test_the_record_carries_no_verdict_field() -> None:
    fields = set(_classify(BASE).model_dump().keys())
    assert not fields & {"safe", "allowed", "verdict", "passed", "ok", "blocked"}


# ── Facet ────────────────────────────────────────────────────────────────


def test_the_facet_is_a_list_and_round_trips() -> None:
    changes = [_classify(BASE), _classify(BASE)]
    capsule: dict = {"run_id": "r1"}
    attached = attach_facet(capsule, changes)

    assert capsule == {"run_id": "r1"}  # not mutated
    assert len(attached["facets"][FACET_NAME]) == 2
    read_back = facet_from_capsule(attached)
    assert read_back is not None and len(read_back) == 2


def test_attaching_nothing_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r1"}
    assert attach_facet(capsule, None) == capsule
    assert attach_facet(capsule, []) == capsule


def test_an_invalid_facet_is_reported_not_silently_dropped() -> None:
    with pytest.raises(ToolSchemaChangeError, match=f"invalid {FACET_NAME} facet"):
        facet_from_capsule({"facets": {FACET_NAME: [{"tool_id": "t"}]}})


def test_a_capsule_without_the_facet_reads_as_none() -> None:
    assert facet_from_capsule({"run_id": "r1"}) is None
    assert facet_from_capsule({"facets": {}}) is None
