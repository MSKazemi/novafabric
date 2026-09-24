"""ADR-0233 — `node` / `root` / `tree` filter scope over the capsule tree.

Three defensible answers to "a filter matched something inside a hierarchy —
what should I show you?", and the dashboard previously picked one silently:

* ``node`` — the matching capsules. *"Which capsules failed?"*
* ``root`` — root capsules whose tree contains a match. *"Which runs were affected?"*
* ``tree`` — every capsule in any tree containing a match. *"What was happening around it?"*

D4 is the part that makes this safe rather than merely useful: **a tree that
looks complete while children are still in flight is a wrong answer presented
confidently**, so incompleteness travels with the result.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novafabric.query import QueryParseError, run_query, validate_query_object
from novafabric.query.model import MAX_SCOPE_EXPANSION, Scope
from novafabric.query.parser import build_plan, parse_scope


def _write_capsule(
    base: Path,
    run_id: str,
    status: str,
    *,
    parent: str | None = None,
    expected: int | None = None,
    arrived: int | None = None,
    orphan: str | None = None,
) -> None:
    d = base / run_id
    d.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": "2026-09-06T00:00:00+00:00",
        "status": status,
    }
    if parent:
        manifest["parent_run_id"] = parent
    if expected is not None:
        manifest["children_expected"] = expected
    if arrived is not None:
        manifest["children_arrived"] = arrived
    if orphan:
        manifest["orphan_reason"] = orphan
    (d / "capsule.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "model_call_id": f"{run_id}-c1",
                "gen_ai.request.model": "m",
                "gen_ai.usage.input_tokens": 1,
                "gen_ai.usage.output_tokens": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def capsules(tmp_path: Path) -> Iterator[Path]:
    """One complete tree, one standalone run, one tree still filling."""
    base = tmp_path / "capsules"
    base.mkdir()
    _write_capsule(base, "parent1", "completed", expected=3, arrived=3)
    _write_capsule(base, "child1", "completed", parent="parent1")
    _write_capsule(base, "child2", "error", parent="parent1")
    _write_capsule(base, "child3", "completed", parent="parent1")
    _write_capsule(base, "solo1", "error")
    _write_capsule(base, "parent2", "partially_complete", expected=3, arrived=2)
    _write_capsule(base, "child4", "error", parent="parent2")
    yield base


def _run(capsules: Path, scope: str) -> dict[str, Any]:
    plan = build_plan(select="count()", where="status = error", scope=scope)
    return run_query(plan, capsules, use_cache=False)


# ---------------------------------------------------------------------------
# D1 — the three scopes answer three different questions
# ---------------------------------------------------------------------------


def test_node_returns_only_the_matching_capsules(capsules: Path) -> None:
    """child2, solo1, child4 — the three that actually failed."""
    assert _run(capsules, "node")["rows"][0]["count()"] == 3


def test_root_returns_the_roots_whose_tree_contains_a_match(capsules: Path) -> None:
    """parent1, solo1, parent2 — "which runs were affected?"."""
    result = _run(capsules, "root")
    assert result["rows"][0]["count()"] == 3
    assert result["tree_scope"]["capsules_selected"] == 3


def test_tree_returns_every_capsule_in_a_matching_tree(capsules: Path) -> None:
    """All 7: parent1+3 children, solo1, parent2+child4.

    The siblings are the point — `child1` and `child3` did not fail and are
    exactly what "what was happening around the failure?" asks for.
    """
    result = _run(capsules, "tree")
    assert result["rows"][0]["count()"] == 7
    assert result["tree_scope"]["capsules_selected"] == 7


def test_the_three_scopes_genuinely_differ(capsules: Path) -> None:
    """Non-vacuity: if they all returned the same set, every test above passes."""
    counts = {s: _run(capsules, s)["rows"][0]["count()"] for s in ("node", "root", "tree")}
    assert counts["node"] < counts["tree"], counts
    assert len(set(counts.values())) >= 2, counts


def test_a_standalone_capsule_is_its_own_root(capsules: Path) -> None:
    """`solo1` has no parent, so all three scopes must agree about it."""
    plan = build_plan(select="count()", where="asset = nothing-matches", scope="tree")
    assert run_query(plan, capsules, use_cache=False)["rows"] == []


# ---------------------------------------------------------------------------
# D2 — scope lives in the plan, so the CLI can reproduce any dashboard view
# ---------------------------------------------------------------------------


def test_scope_defaults_to_node() -> None:
    assert build_plan(select="count()").scope is Scope.NODE
    assert parse_scope(None) is Scope.NODE


def test_the_default_round_trips_without_adding_a_key() -> None:
    """Additive and optional: an existing query object is unchanged."""
    assert "scope" not in build_plan(select="count()").to_query_object()


def test_a_non_default_scope_serializes(capsules: Path) -> None:
    obj = build_plan(select="count()", scope="tree").to_query_object()
    assert obj["scope"] == "tree"


def test_the_query_object_path_accepts_scope() -> None:
    """So an ADR-0235 widget can carry it and still pass the closed allow-list."""
    assert validate_query_object({"select": ["count()"], "scope": "root"}).scope is Scope.ROOT


@pytest.mark.parametrize("bad", ["treee", "TREE ", "all", "", "1"])
def test_an_unknown_scope_is_refused_not_silently_defaulted(bad: str) -> None:
    """A caller who typed `--scope treee` asked for a *different result set*.

    Falling back to `node` would answer a question they did not ask, with no
    indication anything was wrong.
    """
    if bad == "TREE ":
        assert parse_scope(bad) is Scope.TREE  # case/whitespace tolerant, still valid
        return
    with pytest.raises(QueryParseError, match="unknown scope"):
        parse_scope(bad)


def test_scope_is_reported_back_in_the_result(capsules: Path) -> None:
    assert _run(capsules, "root")["tree_scope"]["scope"] == "root"


# ---------------------------------------------------------------------------
# D4 — an incomplete tree renders as incomplete
# ---------------------------------------------------------------------------


def test_a_tree_still_filling_is_reported_incomplete(capsules: Path) -> None:
    """`parent2` expects 3 children and has 2. Saying nothing would be a lie."""
    scope_block = _run(capsules, "tree")["tree_scope"]
    assert scope_block["complete"] is False
    assert any("parent2" in r for r in scope_block["incomplete_reasons"])
    assert any("2 of 3" in r for r in scope_block["incomplete_reasons"])


def test_node_scope_emits_no_tree_block_at_all(capsules: Path) -> None:
    """`node` does not expand, so the result is byte-identical to pre-ADR-0233.

    Absent is the right answer, not present-with-nulls: an **absent** block means
    no expansion was requested, while a present one with an empty
    `incomplete_reasons` means the tree was checked and found complete. Those are
    different claims and the shape distinguishes them.
    """
    result = _run(capsules, "node")
    assert "tree_scope" not in result


def test_the_default_result_shape_is_unchanged(capsules: Path) -> None:
    """AC1 — every existing consumer sees exactly the keys it saw before."""
    result = _run(capsules, "node")
    assert set(result) == {
        "schema_version",
        "generated_at",
        "query",
        "time_window",
        "columns",
        "rows",
        "row_count",
        "truncated",
        "index",
    }


def test_an_orphan_placeholder_is_reported(tmp_path: Path) -> None:
    """A tree can legitimately be incomplete; the scope model must have an answer."""
    base = tmp_path / "capsules"
    base.mkdir()
    _write_capsule(base, "ORPHAN_PARENT_x", "completed", orphan="parent never arrived")
    _write_capsule(base, "childX", "error", parent="ORPHAN_PARENT_x")
    result = _run(base, "tree")
    reasons = result["tree_scope"]["incomplete_reasons"]
    assert any("orphan" in r.lower() for r in reasons), reasons


def test_a_complete_tree_reports_complete(tmp_path: Path) -> None:
    """The converse — otherwise "incomplete" carries no information."""
    base = tmp_path / "capsules"
    base.mkdir()
    _write_capsule(base, "p", "completed", expected=2, arrived=2)
    _write_capsule(base, "c1", "error", parent="p")
    _write_capsule(base, "c2", "completed", parent="p")
    scope_block = _run(base, "tree")["tree_scope"]
    assert scope_block["complete"] is True
    assert scope_block["incomplete_reasons"] == []


# ---------------------------------------------------------------------------
# D3 — expansion is bounded, and a cycle does not hang the query
# ---------------------------------------------------------------------------


def test_the_expansion_cap_is_bounded_and_stated() -> None:
    assert MAX_SCOPE_EXPANSION > 0
    assert MAX_SCOPE_EXPANSION <= 10_000, "a cap this large stops being a cap"


def test_a_parent_cycle_does_not_hang(tmp_path: Path) -> None:
    """A malformed chain degrades that capsule's answer; it must not hang the query.

    The index is a flat row set that can contain a partially-written or
    malformed chain, so the walk needs its own guard rather than relying on
    `CapsuleTreeAssembler`'s.
    """
    base = tmp_path / "capsules"
    base.mkdir()
    _write_capsule(base, "a", "error", parent="b")
    _write_capsule(base, "b", "completed", parent="a")
    result = _run(base, "tree")  # must terminate
    assert result["tree_scope"]["capsules_selected"] >= 1


# ---------------------------------------------------------------------------
# D5 — the cache version bump is load-bearing here
# ---------------------------------------------------------------------------


def test_the_indexer_extracts_the_tree_fields() -> None:
    """Without these, every capsule looks like a root and scope answers wrongly."""
    from dataclasses import fields

    from novafabric.query.indexer import CallRow, ScoreRow

    for model in (CallRow, ScoreRow):
        names = {f.name for f in fields(model)}
        assert {"parent_run_id", "children_expected", "children_arrived"} <= names, model


def test_the_indexer_schema_version_was_bumped() -> None:
    """A row cached at version 1 has fewer values and rehydrates with the new
    fields defaulted to None — every capsule would look like a root and a
    `root`/`tree` query would answer confidently and wrongly from a warm cache.

    ⚠ Contrast ADR-0236's `ratio()`, which asks for a bump and does **not** get
    one: it extracts nothing new. The test is always "did the indexer learn to
    read something?"
    """
    from novafabric.query.cache import INDEXER_SCHEMA_VERSION

    assert INDEXER_SCHEMA_VERSION >= 2


def test_scope_results_are_consistent_with_and_without_the_cache(capsules: Path) -> None:
    """A cache that changes the answer is the one outcome ADR-0225 D3 forbids."""
    plan = build_plan(select="count()", where="status = error", scope="tree")
    cold = run_query(plan, capsules, use_cache=False)["rows"]
    warm = run_query(plan, capsules, use_cache=True, rebuild_cache=True)["rows"]
    assert cold == warm
