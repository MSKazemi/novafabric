"""ADR-0141 + ADR-0129: select export members with a query filter.

`export-blob` could select capsules explicitly or by time window; the
manifest's `query` provenance field existed but could only ever hold a
time expression. This wires the ADR-0129 query DSL as a **filter**.

Design note pinned by these tests: the query's `where` clause selects, not
its grouping. `run_id` is not an allow-listed DSL dimension, so "group by
run_id" is not expressible — and adding it would widen a documented public
surface and invite the high-cardinality grouping the DSL's cap exists to
prevent. Filtering needs neither.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from novafabric.export_blob.service import ExportSelectionError, select_capsules


def _capsule(root: Path, run_id: str, *, model: str, status: str = "success") -> Path:
    """A capsule with one indexable model call, so queries can filter it."""
    capsule = root / run_id
    capsule.mkdir(parents=True)
    (capsule / "capsule.yaml").write_text(
        yaml.dump(
            {
                "run_id": run_id,
                "created_at": "2026-07-01T00:00:00Z",
                "status": status,
            }
        )
    )
    (capsule / "model-calls.jsonl").write_text(
        json.dumps(
            {
                # The indexer reads the OTel GenAI attribute names, not bare
                # "model" — see query/indexer.py::_model_call_rows.
                "gen_ai.response.model": model,
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 5,
            }
        )
        + "\n"
    )
    return capsule


@pytest.fixture()
def capsule_root(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(root, "run-a", model="gpt-4o")
    _capsule(root, "run-b", model="gpt-4o")
    _capsule(root, "run-c", model="claude-opus-4")
    return root


def test_query_selects_only_matching_capsules(capsule_root: Path) -> None:
    selection = select_capsules(root=capsule_root, query="model = 'gpt-4o'")
    assert sorted(d.name for d in selection.capsule_dirs) == ["run-a", "run-b"]


def test_query_excluding_everything_selects_nothing(capsule_root: Path) -> None:
    selection = select_capsules(root=capsule_root, query="model = 'no-such-model'")
    assert selection.capsule_dirs == []


def test_no_query_still_selects_everything(capsule_root: Path) -> None:
    """The pre-existing behaviour must be untouched."""
    selection = select_capsules(root=capsule_root)
    assert len(selection.capsule_dirs) == 3
    assert selection.query == "*"


def test_query_provenance_records_the_parsed_query_not_the_raw_string(
    capsule_root: Path,
) -> None:
    """A manifest reader should not have to re-parse free text."""
    selection = select_capsules(root=capsule_root, query="model = 'gpt-4o'")
    assert selection.query is not None
    assert "query=" in selection.query
    # The recorded value is the canonical query object.
    recorded = selection.query.split("query=", 1)[1]
    parsed = json.loads(recorded)
    assert isinstance(parsed, dict)
    assert "where" in parsed


def test_query_composes_with_the_time_window(capsule_root: Path) -> None:
    """A query narrows the time window; it does not replace it."""
    selection = select_capsules(
        root=capsule_root, since="2020-01-01", query="model = 'gpt-4o'"
    )
    assert sorted(d.name for d in selection.capsule_dirs) == ["run-a", "run-b"]
    assert selection.query is not None
    assert "created_at >=" in selection.query  # window recorded...
    assert "query=" in selection.query  # ...alongside the filter


def test_time_window_can_exclude_what_the_query_matched(tmp_path: Path) -> None:
    """Composition means BOTH must hold, not either."""
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(root, "run-old", model="gpt-4o")
    selection = select_capsules(
        root=root, since="2099-01-01", query="model = 'gpt-4o'"
    )
    assert selection.capsule_dirs == []


def test_query_is_mutually_exclusive_with_explicit_capsules(
    capsule_root: Path,
) -> None:
    with pytest.raises(ExportSelectionError, match="mutually exclusive"):
        select_capsules(["run-a"], root=capsule_root, query="model = 'gpt-4o'")


def test_invalid_query_is_a_clean_selection_error(capsule_root: Path) -> None:
    """A DSL parse failure must not surface as a raw QueryError traceback."""
    with pytest.raises(ExportSelectionError, match="invalid --query"):
        select_capsules(root=capsule_root, query="not_a_field = 'x'")


def test_unknown_dimension_names_the_allowed_ones(capsule_root: Path) -> None:
    with pytest.raises(ExportSelectionError) as exc:
        select_capsules(root=capsule_root, query="bogus = 'x'")
    assert "allowed" in str(exc.value)
