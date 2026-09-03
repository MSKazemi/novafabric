"""NF-169 tool-deprecation lineage (ADR-0148 D2).

The load-bearing case is the *third* answer. `tool_version` is required by the tool-call schema
but is documented as *"Semver if known; `unknown` otherwise"*, so a run recorded that way can
neither be confirmed as pinned to a retired version nor cleared of it. Folding it in over-reports;
dropping it silently under-reports while looking complete.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.supplychain.toolschema.deprecation import (
    FACET_NAME,
    UNKNOWN_VERSION,
    ToolDeprecationError,
    attach_facet,
    build_deprecation,
    facet_from_capsule,
    scan_for_dependents,
)

WHEN = "2026-07-28T00:00:00Z"


def _capsule(
    root: Path, run_id: str, calls: list[tuple[str, str]] | None, *, manifest_run_id: str | None = None
) -> Path:
    d = root / run_id
    d.mkdir(parents=True)
    d.joinpath("capsule.json").write_text(
        json.dumps(
            {
                "run_id": manifest_run_id or run_id,
                "created_at": "2026-07-10T00:00:00Z",
                "status": "success",
            }
        )
    )
    if calls is not None:
        d.joinpath("tool-calls.jsonl").write_text(
            "\n".join(
                json.dumps({"tool_name": name, "tool_version": version, "arguments": {}})
                for name, version in calls
            )
            + "\n"
        )
    return d


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(root, "run-old", [("search", "1.0.0")])
    _capsule(root, "run-new", [("search", "2.0.0")])
    _capsule(root, "run-mixed", [("search", "2.0.0"), ("write", "1.0.0")])
    _capsule(root, "run-unknown", [("search", UNKNOWN_VERSION)])
    _capsule(root, "run-notools", None)
    return root


# ── The three answers ────────────────────────────────────────────────────


def test_only_runs_pinned_to_the_retired_version_are_dependents(store: Path) -> None:
    dependent, unknown, scanned = scan_for_dependents(
        store, tool_id="search", version="1.0.0"
    )
    assert dependent == ["run-old"]
    assert unknown == ["run-unknown"]
    assert scanned == 5


def test_an_unknown_version_is_neither_confirmed_nor_cleared(store: Path) -> None:
    """The load-bearing case: it must appear in exactly one place, and not the dependent list."""
    record = build_deprecation(
        store, tool_id="search", deprecated_version="1.0.0", deprecated_at=WHEN
    )
    assert "run-unknown" not in record.dependent_run_ids
    assert record.unknown_version_run_ids == ["run-unknown"]


def test_a_different_tool_at_the_same_version_is_not_a_dependent(store: Path) -> None:
    """`run-mixed` calls write@1.0.0 — matching on version alone would flag it."""
    record = build_deprecation(
        store, tool_id="search", deprecated_version="1.0.0", deprecated_at=WHEN
    )
    assert record.dependent_run_ids == ["run-old"]


def test_a_run_that_never_called_the_tool_is_in_neither_list(store: Path) -> None:
    record = build_deprecation(
        store, tool_id="search", deprecated_version="1.0.0", deprecated_at=WHEN
    )
    for bucket in (record.dependent_run_ids, record.unknown_version_run_ids):
        assert "run-notools" not in bucket


def test_the_scanned_count_travels_with_the_answer(store: Path) -> None:
    """Without it, "nothing was affected" and "nothing was searched" read the same."""
    record = build_deprecation(
        store, tool_id="search", deprecated_version="9.9.9", deprecated_at=WHEN
    )
    assert record.dependent_run_ids == []
    assert record.capsules_scanned == 5


def test_an_empty_store_reports_zero_scanned(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    record = build_deprecation(
        empty, tool_id="search", deprecated_version="1.0.0", deprecated_at=WHEN
    )
    assert (record.dependent_run_ids, record.capsules_scanned) == ([], 0)


# ── It uses the shared readers, and the shared identity ──────────────────


def test_the_manifest_run_id_wins_over_the_directory_name(tmp_path: Path) -> None:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(root, "dir-name", [("search", "1.0.0")], manifest_run_id="real-run-id")
    dependent, _, _ = scan_for_dependents(root, tool_id="search", version="1.0.0")
    assert dependent == ["real-run-id"]


def test_a_directory_without_a_manifest_is_not_a_capsule(store: Path) -> None:
    (store / "notes").mkdir()
    (store / "notes" / "tool-calls.jsonl").write_text(
        json.dumps({"tool_name": "search", "tool_version": "1.0.0", "arguments": {}}) + "\n"
    )
    _, _, scanned = scan_for_dependents(store, tool_id="search", version="1.0.0")
    assert scanned == 5, "the extra directory is not a capsule"


def test_a_malformed_tool_call_line_is_refused_not_skipped(store: Path) -> None:
    """Inherited from the shared strict reader: a dropped call is a wrong answer, silently."""
    (store / "run-old" / "tool-calls.jsonl").write_text("{not json\n")
    with pytest.raises(ValueError, match="invalid tool-call record"):
        scan_for_dependents(store, tool_id="search", version="1.0.0")


def test_a_missing_capsule_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ToolDeprecationError, match="capsule directory not found"):
        scan_for_dependents(tmp_path / "nope", tool_id="search", version="1.0.0")


# ── The record must be internally honest ─────────────────────────────────


def test_deprecating_the_unknown_version_is_refused(store: Path) -> None:
    """Every run whose version could not be determined would be flagged as pinned to it."""
    with pytest.raises(ToolDeprecationError, match="cannot deprecate version"):
        build_deprecation(
            store, tool_id="search", deprecated_version=UNKNOWN_VERSION, deprecated_at=WHEN
        )


def test_an_unparseable_deprecation_date_is_refused(store: Path) -> None:
    with pytest.raises(ToolDeprecationError, match="not an ISO-8601 timestamp"):
        build_deprecation(
            store, tool_id="search", deprecated_version="1.0.0", deprecated_at="last tuesday"
        )


def test_an_empty_successor_is_refused_but_an_absent_one_is_fine(store: Path) -> None:
    with pytest.raises(ToolDeprecationError, match="successor must name a tool"):
        build_deprecation(
            store, tool_id="search", deprecated_version="1.0.0",
            deprecated_at=WHEN, successor="  ",
        )
    record = build_deprecation(
        store, tool_id="search", deprecated_version="1.0.0", deprecated_at=WHEN
    )
    assert record.successor is None
    assert "successor" not in record.model_dump(exclude_none=True)


def test_a_named_successor_is_kept(store: Path) -> None:
    record = build_deprecation(
        store, tool_id="search", deprecated_version="1.0.0",
        deprecated_at=WHEN, successor="mcp://acme/search@2",
    )
    assert record.successor == "mcp://acme/search@2"


def test_the_record_carries_no_verdict_field(store: Path) -> None:
    fields = set(
        build_deprecation(
            store, tool_id="search", deprecated_version="1.0.0", deprecated_at=WHEN
        ).model_dump().keys()
    )
    assert not fields & {"verdict", "blocked", "passed", "ok", "safe", "action"}


# ── Facet ────────────────────────────────────────────────────────────────


def test_the_facet_round_trips_and_is_additive(store: Path) -> None:
    record = build_deprecation(
        store, tool_id="search", deprecated_version="1.0.0", deprecated_at=WHEN
    )
    capsule: dict = {"run_id": "r1"}
    attached = attach_facet(capsule, record)

    assert capsule == {"run_id": "r1"}
    assert set(attached["facets"]) == {FACET_NAME}
    read_back = facet_from_capsule(attached)
    assert read_back is not None and read_back.dependent_run_ids == ["run-old"]


def test_attaching_nothing_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r1"}
    assert attach_facet(capsule, None) == capsule


def test_an_invalid_facet_is_reported_not_silently_dropped() -> None:
    with pytest.raises(ToolDeprecationError, match=f"invalid {FACET_NAME} facet"):
        facet_from_capsule({"facets": {FACET_NAME: {"tool_id": "t"}}})


def test_a_capsule_without_the_facet_reads_as_none() -> None:
    assert facet_from_capsule({"run_id": "r1"}) is None
    assert facet_from_capsule({"facets": {}}) is None
