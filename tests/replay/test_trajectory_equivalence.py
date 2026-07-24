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

"""ADR-0144 P1 — canonicalization (NF-128) and trajectory equivalence (NF-122)."""

from __future__ import annotations

import pytest

from novafabric.replay.equivalence import (
    ALL_RULES,
    DEFAULT_RULES,
    RULE_COLLAPSE_IDEMPOTENT_RETRIES,
    RULE_NORMALIZE_ARGUMENTS,
    RULE_REORDER_COMMUTABLE,
    RULES_VERSION,
    DivergenceKind,
    MatchMode,
    ToolCall,
    UnknownRuleError,
    canonicalize,
    compare,
)


def tc(name: str, **args: object) -> ToolCall:
    return ToolCall(name=name, arguments=dict(args))


# ── The headline case ─────────────────────────────────────────────────────


def test_dropped_tool_call_diverges_even_when_tokens_would_match() -> None:
    """The failure a byte-diff of the transcript misses (ADR-0144 D2).

    This is the reason behavioral equivalence is not token equivalence: the
    model can narrate the same answer while having skipped the tool call that
    made the answer true.
    """
    baseline = [tc("search", q="x"), tc("fetch", id=1), tc("summarize")]
    replay = [tc("search", q="x"), tc("summarize")]

    result = compare(baseline, replay, mode=MatchMode.set)

    assert not result.equivalent
    assert [s.kind for s in result.divergent_steps] == [DivergenceKind.dropped]
    assert result.divergent_steps[0].baseline is not None
    assert "fetch" in result.divergent_steps[0].baseline


def test_added_tool_call_also_diverges() -> None:
    baseline = [tc("search", q="x")]
    replay = [tc("search", q="x"), tc("delete", id=1)]
    result = compare(baseline, replay, mode=MatchMode.set)
    assert not result.equivalent
    assert [s.kind for s in result.divergent_steps] == [DivergenceKind.added]


# ── Canonicalization: argument formatting ─────────────────────────────────


def test_key_order_is_not_a_difference() -> None:
    a = canonicalize([ToolCall("f", {"b": 2, "a": 1})]).calls
    b = canonicalize([ToolCall("f", {"a": 1, "b": 2})]).calls
    assert compare(a, b).equivalent


def test_argument_values_are_not_coerced() -> None:
    """Normalizing formatting must not normalize away a type error.

    If "1" and 1 compared equal, a replay that passed a string where the
    baseline passed an int would pass — a real defect, silently absorbed.
    """
    a = canonicalize([ToolCall("f", {"n": 1})]).calls
    b = canonicalize([ToolCall("f", {"n": "1"})]).calls
    assert not compare(a, b).equivalent


# ── Canonicalization: idempotent retries ──────────────────────────────────


def test_consecutive_retry_of_declared_idempotent_tool_collapses() -> None:
    calls = [tc("get", id=1), tc("get", id=1), tc("write")]
    result = canonicalize(calls, idempotent=["get"])
    assert [c.name for c in result.calls] == ["get", "write"]
    assert RULE_COLLAPSE_IDEMPOTENT_RETRIES in result.changes


def test_repeat_of_undeclared_tool_is_preserved() -> None:
    """A duplicated non-idempotent call is a duplicated side effect."""
    calls = [tc("charge", amount=10), tc("charge", amount=10)]
    assert len(canonicalize(calls).calls) == 2


def test_non_consecutive_repeat_is_not_a_retry() -> None:
    """Separated by other work, a repeat is a second round trip."""
    calls = [tc("get", id=1), tc("write"), tc("get", id=1)]
    result = canonicalize(calls, idempotent=["get"])
    assert len(result.calls) == 3


def test_repeat_with_different_arguments_is_not_a_retry() -> None:
    calls = [tc("get", id=1), tc("get", id=2)]
    assert len(canonicalize(calls, idempotent=["get"]).calls) == 2


# ── Canonicalization: commutable reordering ───────────────────────────────


def test_nothing_is_reordered_without_a_declared_commutable_set() -> None:
    """Inferred commutativity would normalize away real ordering bugs."""
    calls = [tc("b"), tc("a")]
    result = canonicalize(calls, rules=ALL_RULES)
    assert [c.name for c in result.calls] == ["b", "a"]


def test_declared_commutable_calls_are_sorted() -> None:
    result = canonicalize(
        [tc("read_b"), tc("read_a")],
        rules=ALL_RULES,
        commutable=["read_a", "read_b"],
    )
    assert [c.name for c in result.calls] == ["read_a", "read_b"]
    assert RULE_REORDER_COMMUTABLE in result.changes


def test_non_commutable_call_is_a_reordering_barrier() -> None:
    """Work must never be reordered across a call that is not commutable."""
    result = canonicalize(
        [tc("read_b"), tc("commit"), tc("read_a")],
        rules=ALL_RULES,
        commutable=["read_a", "read_b"],
    )
    assert [c.name for c in result.calls] == ["read_b", "commit", "read_a"]


def test_reordering_is_off_by_default() -> None:
    assert RULE_REORDER_COMMUTABLE not in DEFAULT_RULES


# ── Auditability of the normalization ─────────────────────────────────────


def test_result_records_version_and_applied_rules() -> None:
    result = canonicalize([tc("f")])
    assert result.rules_version == RULES_VERSION
    assert set(result.rules_applied) == set(DEFAULT_RULES)


def test_changes_record_effect_not_intent() -> None:
    """A rule that ran but changed nothing must not appear as a change."""
    result = canonicalize([tc("f", a=1)])
    assert result.changes == {}
    assert RULE_NORMALIZE_ARGUMENTS in result.rules_applied


def test_unknown_rule_is_rejected_not_ignored() -> None:
    """A typo must not silently disable a normalization the caller wanted."""
    with pytest.raises(UnknownRuleError, match="typo_rule"):
        canonicalize([tc("f")], rules=["typo_rule"])


def test_canonicalize_does_not_mutate_the_input() -> None:
    calls = [ToolCall("f", {"b": 2, "a": 1})]
    canonicalize(calls, rules=ALL_RULES, commutable=["f"])
    assert calls[0].arguments == {"b": 2, "a": 1}


# ── Match modes ───────────────────────────────────────────────────────────


def test_set_mode_ignores_order_but_ordered_mode_does_not() -> None:
    a = [tc("x"), tc("y")]
    b = [tc("y"), tc("x")]
    assert compare(a, b, mode=MatchMode.set).equivalent
    assert not compare(a, b, mode=MatchMode.ordered).equivalent


def test_set_mode_is_a_multiset_not_a_set() -> None:
    """Calling a tool twice is not the same behavior as calling it once."""
    assert not compare([tc("x"), tc("x")], [tc("x")], mode=MatchMode.set).equivalent


def test_set_mode_reports_the_unmatched_occurrence_position() -> None:
    result = compare([tc("x")], [tc("x"), tc("x")], mode=MatchMode.set)
    assert [s.index for s in result.divergent_steps] == [1]


def test_edit_mode_tolerates_a_localized_edit_within_tolerance() -> None:
    baseline = [tc("a"), tc("b"), tc("c"), tc("d")]
    replay = [tc("a"), tc("b"), tc("c"), tc("e")]
    strict = compare(baseline, replay, mode=MatchMode.edit)
    lenient = compare(baseline, replay, mode=MatchMode.edit, tolerance=0.25)
    assert not strict.equivalent
    assert lenient.equivalent
    assert strict.distance == pytest.approx(0.25)


def test_ordered_mode_reports_a_positional_change() -> None:
    result = compare([tc("a")], [tc("b")], mode=MatchMode.ordered)
    assert [s.kind for s in result.divergent_steps] == [DivergenceKind.changed]


def test_default_tolerance_is_exact() -> None:
    """Slack has to be asked for, and therefore recorded."""
    result = compare([tc("a")], [tc("b")])
    assert result.tolerance == 0.0
    assert not result.equivalent


# ── Edge cases ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", list(MatchMode))
def test_identical_trajectories_are_equivalent_in_every_mode(mode: MatchMode) -> None:
    calls = [tc("a", x=1), tc("b")]
    result = compare(calls, calls, mode=mode)
    assert result.equivalent
    assert result.distance == 0.0


@pytest.mark.parametrize("mode", list(MatchMode))
def test_two_empty_trajectories_are_equivalent(mode: MatchMode) -> None:
    """A run that called no tools is a legitimate case, not a divide-by-zero."""
    result = compare([], [], mode=mode)
    assert result.equivalent
    assert result.distance == 0.0


@pytest.mark.parametrize("mode", list(MatchMode))
def test_distance_is_bounded(mode: MatchMode) -> None:
    result = compare([tc("a"), tc("b")], [tc("c")], mode=mode)
    assert 0.0 <= result.distance <= 1.0
